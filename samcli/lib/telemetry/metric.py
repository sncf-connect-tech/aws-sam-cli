"""
Provides methods to generate and send metrics
"""
from timeit import default_timer
from functools import wraps, reduce

import uuid
import platform
import logging
import traceback
from typing import Optional, Tuple

import click

from samcli import __version__ as samcli_version
from samcli.cli.context import Context
from samcli.cli.global_config import GlobalConfig
from samcli.lib.warnings.sam_cli_warning import TemplateWarningsChecker
from samcli.commands.exceptions import UserException
from samcli.lib.telemetry.cicd import CICDDetector, CICDPlatform
from samcli.lib.telemetry.event import EventTracker
from samcli.lib.telemetry.project_metadata import get_git_remote_origin_url, get_project_name, get_initial_commit_hash
from samcli.commands._utils.experimental import get_all_experimental_statues
from .telemetry import Telemetry
from ..iac.cdk.utils import is_cdk_project
from ..iac.plugins_interfaces import ProjectTypes

LOG = logging.getLogger(__name__)

WARNING_ANNOUNCEMENT = "WARNING: {}"

"""
Global variables are evil but this is a justified usage.
This creates a versatile telemetry tracking no matter where in the code. Something like a Logger.
No side effect will result in this as it is write-only for code outside of telemetry.
Decorators should be used to minimize logic involving telemetry.
"""
_METRICS = dict()


def send_installed_metric():
    LOG.debug("Sending Installed Metric")

    telemetry = Telemetry()
    metric = Metric("installed")
    metric.add_data("osPlatform", platform.system())
    metric.add_data("telemetryEnabled", bool(GlobalConfig().telemetry_enabled))
    telemetry.emit(metric, force_emit=True)


def track_template_warnings(warning_names):
    """
    Decorator to track when a warning is emitted. This method accepts name of warning and executes the function,
    gathers all relevant metrics, reports the metrics and returns.

    On your warning check method use as follows

        @track_warning(['Warning1', 'Warning2'])
        def check():
            return True, 'Warning applicable'
    """

    def decorator(func):
        """
        Actual decorator method with warning names
        """

        def wrapped(*args, **kwargs):
            telemetry = Telemetry()
            template_warning_checker = TemplateWarningsChecker()
            ctx = Context.get_current_context()

            try:
                ctx.template_dict
            except AttributeError:
                LOG.debug("Ignoring warning check as template is not provided in context.")
                return func(*args, **kwargs)
            for warning_name in warning_names:
                warning_message = template_warning_checker.check_template_for_warning(warning_name, ctx.template_dict)
                metric = Metric("templateWarning")
                metric.add_data("awsProfileProvided", bool(ctx.profile))
                metric.add_data("debugFlagProvided", bool(ctx.debug))
                metric.add_data("region", ctx.region or "")
                metric.add_data("warningName", warning_name)
                metric.add_data("warningCount", 1 if warning_message else 0)  # 1-True or 0-False
                telemetry.emit(metric)

                if warning_message:
                    click.secho(WARNING_ANNOUNCEMENT.format(warning_message), fg="yellow")

            return func(*args, **kwargs)

        return wrapped

    return decorator


def track_command(func):
    """
    Decorator to track execution of a command. This method executes the function, gathers all relevant metrics,
    reports the metrics and returns.

    If you have a Click command, you can track as follows:

    .. code:: python
        @click.command(...)
        @click.options(...)
        @track_command
        def hello_command():
            print('hello')

    """

    def wrapped(*args, **kwargs):
        telemetry = Telemetry()

        exception = None
        return_value = None
        exit_reason = "success"
        exit_code = 0
        stack_trace = None
        exception_message = None

        duration_fn = _timer()
        try:

            # Execute the function and capture return value. This is returned back by the wrapper
            # First argument of all commands should be the Context
            return_value = func(*args, **kwargs)

        except UserException as ex:
            # Capture exception information and re-raise it later so we can first send metrics.
            exception = ex
            exit_code = ex.exit_code
            if ex.wrapped_from is None:
                exit_reason = type(ex).__name__
            else:
                exit_reason = ex.wrapped_from
            stack_trace, exception_message = _get_stack_trace_info(ex)

        except Exception as ex:
            exception = ex
            # Standard Unix practice to return exit code 255 on fatal/unhandled exit.
            exit_code = 255
            exit_reason = type(ex).__name__
            stack_trace, exception_message = _get_stack_trace_info(ex)

        try:
            ctx = Context.get_current_context()
            metric_specific_attributes = get_all_experimental_statues() if ctx.experimental else {}
            try:
                template_dict = ctx.template_dict
                project_type = ProjectTypes.CDK.value if is_cdk_project(template_dict) else ProjectTypes.CFN.value
                if project_type == ProjectTypes.CDK.value:
                    EventTracker.track_event("UsedFeature", "CDK")
                metric_specific_attributes["projectType"] = project_type
            except AttributeError:
                LOG.debug("Template is not provided in context, skip adding project type metric")
            metric_name = "commandRunExperimental" if ctx.experimental else "commandRun"
            metric = Metric(metric_name)
            metric.add_data("awsProfileProvided", bool(ctx.profile))
            metric.add_data("debugFlagProvided", bool(ctx.debug))
            metric.add_data("region", ctx.region or "")
            metric.add_data("commandName", ctx.command_path)  # Full command path. ex: sam local start-api
            if not ctx.command_path.endswith("init") or ctx.command_path.endswith("pipeline init"):
                # Project metadata
                # We don't capture below usage attributes for sam init as the command is not run inside a project
                metric_specific_attributes["gitOrigin"] = get_git_remote_origin_url()
                metric_specific_attributes["projectName"] = get_project_name()
                metric_specific_attributes["initialCommit"] = get_initial_commit_hash()
            metric.add_data("metricSpecificAttributes", metric_specific_attributes)
            # Metric about command's execution characteristics
            metric.add_data("duration", duration_fn())
            metric.add_data("exitReason", exit_reason)
            metric.add_data("exitCode", exit_code)
            metric.add_data("stackTrace", stack_trace)
            metric.add_data("exceptionMessage", exception_message)
            EventTracker.send_events()  # Sends Event metrics to Telemetry before commandRun metrics
            telemetry.emit(metric)
        except RuntimeError:
            LOG.debug("Unable to find Click Context for getting session_id.")
        if exception:
            raise exception  # pylint: disable=raising-bad-type

        return return_value

    return wrapped


def _get_stack_trace_info(exception: Exception) -> Tuple[str, str]:
    """
    Takes an Exception instance and extracts the following:
      1. Stack trace in a readable string format with user-sensitive paths cleaned
      2. Exception mesage including the fully-qualified exception name and value

    Parameters
    ----------
    exception : Exception
        Exception instance

    Returns
    -------
    (str, str)
        (stack trace, exception message)
    """
    tb_exception = traceback.TracebackException.from_exception(exception)
    _clean_stack_summary_paths(tb_exception.stack)
    stack_trace = "".join(list(tb_exception.format()))
    exception_msg = list(tb_exception.format_exception_only())[-1]

    return (stack_trace, exception_msg)


def _clean_stack_summary_paths(stack_summary: traceback.StackSummary) -> None:
    """
    Cleans the user-sensitive paths contained within a StackSummary instance

    Parameters
    ----------
    stack_summary : traceback.StackSummary
        StackSummary instance
    """
    for frame in stack_summary:
        path = frame.filename
        separator = "\\" if "\\" in path else "/"

        # Case 1: If "site-packages" is found within path, replace its leading segment with: /../ or \..\
        # i.e. /python3.8/site-packages/boto3/test.py becomes /../site-packages/boto3/test.py
        site_packages_idx = path.rfind("site-packages")
        if site_packages_idx != -1:
            frame.filename = f"{separator}..{separator}{path[site_packages_idx:]}"
            continue

        # Case 2: If "samcli" is found within path, do the same replacement as previous
        samcli_idx = path.rfind("samcli")
        if samcli_idx != -1:
            frame.filename = f"{separator}..{separator}{path[samcli_idx:]}"
            continue

        # Case 3: Keep only the last file within the path, and do the same replacement as previous
        path_split = path.split(separator)
        if len(path_split) > 0:
            frame.filename = f"{separator}..{separator}{path_split[-1]}"


def _timer():
    """
    Timer to measure the elapsed time between two calls in milliseconds. When you first call this method,
    we will automatically start the timer. The return value is another method that, when called, will end the timer
    and return the duration between the two calls.

    ..code:
    >>> import time
    >>> duration_fn = _timer()
    >>> time.sleep(5)  # Say, you sleep for 5 seconds in between calls
    >>> duration_ms = duration_fn()
    >>> print(duration_ms)
        5010

    Returns
    -------
    function
        Call this method to end the timer and return duration in milliseconds

    """
    start = default_timer()

    def end():
        # time might go backwards in rare scenarios, hence the 'max'
        return int(max(default_timer() - start, 0) * 1000)  # milliseconds

    return end


def _parse_attr(obj, name):
    """
    Get attribute from an object.
    @param obj Object
    @param name Attribute name to get from the object.
        Can be nested with "." in between.
        For example: config.random_field.value
    """
    return reduce(getattr, name.split("."), obj)


def capture_parameter(metric_name, key, parameter_identifier, parameter_nested_identifier=None, as_list=False):
    """
    Decorator for capturing one parameter of the function.

    :param metric_name Name of the metric
    :param key Key for storing the captured parameter
    :param parameter_identifier Either a string for named parameter or int for positional parameter.
        "self" can be accessed with 0.
    :param parameter_nested_identifier If specified, the attribute pointed by this parameter will be stored instead.
        Can be in nested format such as config.random_field.value.
    :param as_list Default to False. Setting to True will append the captured parameter into
        a list instead of overriding the previous one.
    """

    def wrap(func):
        @wraps(func)
        def wrapped_func(*args, **kwargs):
            return_value = func(*args, **kwargs)
            if isinstance(parameter_identifier, int):
                parameter = args[parameter_identifier]
            elif isinstance(parameter_identifier, str):
                parameter = kwargs[parameter_identifier]
            else:
                return return_value

            if parameter_nested_identifier:
                parameter = _parse_attr(parameter, parameter_nested_identifier)

            if as_list:
                add_metric_list_data(metric_name, key, parameter)
            else:
                add_metric_data(metric_name, key, parameter)
            return return_value

        return wrapped_func

    return wrap


def capture_return_value(metric_name, key, as_list=False):
    """
    Decorator for capturing the return value of the function.

    :param metric_name Name of the metric
    :param key Key for storing the captured parameter
    :param as_list Default to False. Setting to True will append the captured parameter into
        a list instead of overriding the previous one.
    """

    def wrap(func):
        @wraps(func)
        def wrapped_func(*args, **kwargs):
            return_value = func(*args, **kwargs)
            if as_list:
                add_metric_list_data(metric_name, key, return_value)
            else:
                add_metric_data(metric_name, key, return_value)
            return return_value

        return wrapped_func

    return wrap


def add_metric_data(metric_name, key, value):
    _get_metric(metric_name).add_data(key, value)


def add_metric_list_data(metric_name, key, value):
    _get_metric(metric_name).add_list_data(key, value)


def _get_metric(metric_name):
    if metric_name not in _METRICS:
        _METRICS[metric_name] = Metric(metric_name)
    return _METRICS[metric_name]


def emit_metric(metric_name):
    if metric_name not in _METRICS:
        return
    telemetry = Telemetry()
    telemetry.emit(_get_metric(metric_name))
    _METRICS.pop(metric_name)


def emit_all_metrics():
    for key in list(_METRICS):
        emit_metric(key)


class Metric:
    """
    Metric class to store metric data and adding common attributes
    """

    def __init__(self, metric_name, should_add_common_attributes=True):
        self._data = dict()
        self._metric_name = metric_name
        self._gc = GlobalConfig()
        self._session_id = self._default_session_id()
        self._cicd_detector = CICDDetector()
        if not self._session_id:
            self._session_id = ""
        if should_add_common_attributes:
            self._add_common_metric_attributes()

    def add_list_data(self, key, value):
        if key not in self._data:
            self._data[key] = list()

        if not isinstance(self._data[key], list):
            # raise MetricDataNotList()
            return

        self._data[key].append(value)

    def add_data(self, key, value):
        self._data[key] = value

    def get_data(self):
        return self._data

    def get_metric_name(self):
        return self._metric_name

    def _add_common_metric_attributes(self):
        self._data["requestId"] = str(uuid.uuid4())
        self._data["installationId"] = self._gc.installation_id
        self._data["sessionId"] = self._session_id
        self._data["executionEnvironment"] = self._get_execution_environment()
        self._data["ci"] = bool(self._cicd_detector.platform())
        self._data["pyversion"] = platform.python_version()
        self._data["samcliVersion"] = samcli_version

    @staticmethod
    def _default_session_id() -> Optional[str]:
        """
        Get the default SessionId from Click Context.
        Fail silently if Context does not exist.
        """
        try:
            ctx = Context.get_current_context()
            if ctx:
                return ctx.session_id
            return None
        except RuntimeError:
            LOG.debug("Unable to find Click Context for getting session_id.")
            return None

    def _get_execution_environment(self) -> str:
        """
        Returns the environment in which SAM CLI is running. Possible options are:

        CLI (default)               - SAM CLI was executed from terminal or a script.
        other CICD platform name    - SAM CLI was executed in CICD

        Returns
        -------
        str
            Name of the environment where SAM CLI is executed in.
        """
        cicd_platform: Optional[CICDPlatform] = self._cicd_detector.platform()
        if cicd_platform:
            return cicd_platform.name
        return "CLI"


class MetricDataNotList(Exception):
    pass
