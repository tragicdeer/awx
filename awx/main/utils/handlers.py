# Copyright (c) 2017 Ansible Tower by Red Hat
# All Rights Reserved.

# Python
import logging
import json
import requests
import time
import urlparse
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from requests.exceptions import RequestException

# loggly
import traceback

from requests_futures.sessions import FuturesSession

# AWX
from awx.main.utils.formatters import LogstashFormatter


__all__ = ['HTTPSNullHandler', 'BaseHTTPSHandler', 'configure_external_logger']

logger = logging.getLogger('awx.main.utils.handlers')

# AWX external logging handler, generally designed to be used
# with the accompanying LogstashHandler, derives from python-logstash library
# Non-blocking request accomplished by FuturesSession, similar
# to the loggly-python-handler library (not used)

# Translation of parameter names to names in Django settings
PARAM_NAMES = {
    'host': 'LOG_AGGREGATOR_HOST',
    'port': 'LOG_AGGREGATOR_PORT',
    'message_type': 'LOG_AGGREGATOR_TYPE',
    'username': 'LOG_AGGREGATOR_USERNAME',
    'password': 'LOG_AGGREGATOR_PASSWORD',
    'enabled_loggers': 'LOG_AGGREGATOR_LOGGERS',
    'indv_facts': 'LOG_AGGREGATOR_INDIVIDUAL_FACTS',
    'enabled_flag': 'LOG_AGGREGATOR_ENABLED',
    'http_timeout': 'LOG_AGGREGATOR_HTTP_TIMEOUT',
}


def unused_callback(sess, resp):
    pass


class LoggingConnectivityException(Exception):
    pass


class HTTPSNullHandler(logging.NullHandler):
    "Placeholder null handler to allow loading without database access"

    def __init__(self, *args, **kwargs):
        return super(HTTPSNullHandler, self).__init__()


class VerboseThreadPoolExecutor(ThreadPoolExecutor):

    last_log_emit = 0

    def submit(self, func, *args, **kwargs):
        def _wrapped(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception:
                # If an exception occurs in a concurrent thread worker (like
                # a ConnectionError or a read timeout), periodically log
                # that failure.
                #
                # This approach isn't really thread-safe, so we could
                # potentially log once per thread every 10 seconds, but it
                # beats logging *every* failed HTTP request in a scenario where
                # you've typo'd your log aggregator hostname.
                now = time.time()
                if now - self.last_log_emit > 10:
                    logger.exception('failed to emit log to external aggregator')
                    self.last_log_emit = now
                raise
        return super(VerboseThreadPoolExecutor, self).submit(_wrapped, *args,
                                                             **kwargs)


class BaseHTTPSHandler(logging.Handler):
    def __init__(self, fqdn=False, **kwargs):
        super(BaseHTTPSHandler, self).__init__()
        self.fqdn = fqdn
        for fd in PARAM_NAMES:
            setattr(self, fd, kwargs.get(fd, None))
        self.session = FuturesSession(executor=VerboseThreadPoolExecutor(
            max_workers=2  # this is the default used by requests_futures
        ))
        self.add_auth_information()

    @classmethod
    def from_django_settings(cls, settings, *args, **kwargs):
        for param, django_setting_name in PARAM_NAMES.items():
            kwargs[param] = getattr(settings, django_setting_name, None)
        return cls(*args, **kwargs)

    @classmethod
    def perform_test(cls, settings):
        """
        Tests logging connectivity for the current logging settings.
        @raises LoggingConnectivityException
        """
        handler = cls.from_django_settings(settings, async=True)
        handler.enabled_flag = True
        handler.setFormatter(LogstashFormatter(settings_module=settings))
        logger = logging.getLogger(__file__)
        fn, lno, func = logger.findCaller()
        record = logger.makeRecord('awx', 10, fn, lno,
                                   'Ansible Tower Connection Test', tuple(),
                                   None, func)
        futures = handler.emit(record)
        for future in futures:
            try:
                resp = future.result()
                if not resp.ok:
                    raise LoggingConnectivityException(
                        ': '.join([str(resp.status_code), resp.reason or ''])
                    )
            except RequestException as e:
                raise LoggingConnectivityException(str(e))

    def get_full_message(self, record):
        if record.exc_info:
            return '\n'.join(traceback.format_exception(*record.exc_info))
        else:
            return record.getMessage()

    def add_auth_information(self):
        if self.message_type == 'logstash':
            if not self.username:
                # Logstash authentication not enabled
                return
            logstash_auth = requests.auth.HTTPBasicAuth(self.username, self.password)
            self.session.auth = logstash_auth
        elif self.message_type == 'splunk':
            auth_header = "Splunk %s" % self.password
            headers = {
                "Authorization": auth_header,
                "Content-Type": "application/json"
            }
            self.session.headers.update(headers)

    def get_http_host(self):
        host = self.host or ''
        # urlparse requires scheme to be provided, default to use http if
        # missing
        if not urlparse.urlsplit(host).scheme:
            host = 'http://%s' % host
        parsed = urlparse.urlsplit(host)
        # Insert self.port if its special and port number is either not
        # given in host or given as non-numerical
        try:
            port = parsed.port or self.port
        except ValueError:
            port = self.port
        if port not in (80, None):
            new_netloc = '%s:%s' % (parsed.hostname, port)
            return urlparse.urlunsplit((parsed.scheme, new_netloc, parsed.path,
                                        parsed.query, parsed.fragment))
        return host

    def get_post_kwargs(self, payload_input):
        if self.message_type == 'splunk':
            # Splunk needs data nested under key "event"
            if not isinstance(payload_input, dict):
                payload_input = json.loads(payload_input)
            payload_input = {'event': payload_input}
        if isinstance(payload_input, dict):
            payload_str = json.dumps(payload_input)
        else:
            payload_str = payload_input
        return dict(data=payload_str, background_callback=unused_callback,
                    timeout=self.http_timeout)

    def skip_log(self, logger_name):
        if self.host == '' or (not self.enabled_flag):
            return True
        if not logger_name.startswith('awx.analytics'):
            # Tower log emission is only turned off by enablement setting
            return False
        return self.enabled_loggers is None or logger_name[len('awx.analytics.'):] not in self.enabled_loggers

    def emit(self, record):
        """
            Emit a log record.  Returns a list of zero or more
            ``concurrent.futures.Future`` objects.

            See:
            https://docs.python.org/3/library/concurrent.futures.html#future-objects
            http://pythonhosted.org/futures/
        """
        if self.skip_log(record.name):
            return []
        try:
            payload = self.format(record)

            # Special action for System Tracking, queue up multiple log messages
            if self.indv_facts:
                payload_data = json.loads(payload)
                if record.name.startswith('awx.analytics.system_tracking'):
                    module_name = payload_data['module_name']
                    if module_name in ['services', 'packages', 'files']:
                        facts_dict = payload_data.pop(module_name)
                        async_futures = []
                        for key in facts_dict:
                            fact_payload = copy(payload_data)
                            fact_payload.update(facts_dict[key])
                            async_futures.append(self._send(fact_payload))
                        return async_futures

            return [self._send(payload)]
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            self.handleError(record)

    def _send(self, payload):
        return self.session.post(self.get_http_host(),
                                 **self.get_post_kwargs(payload))


def add_or_remove_logger(address, instance):
    specific_logger = logging.getLogger(address)
    for i, handler in enumerate(specific_logger.handlers):
        if isinstance(handler, (HTTPSNullHandler, BaseHTTPSHandler)):
            specific_logger.handlers[i] = instance or HTTPSNullHandler()
            break
    else:
        if instance is not None:
            specific_logger.handlers.append(instance)


def configure_external_logger(settings_module, is_startup=True):

    is_enabled = settings_module.LOG_AGGREGATOR_ENABLED
    if is_startup and (not is_enabled):
        # Pass-through if external logging not being used
        return

    instance = None
    if is_enabled:
        instance = BaseHTTPSHandler.from_django_settings(settings_module)
        instance.setFormatter(LogstashFormatter(settings_module=settings_module))
    awx_logger_instance = instance
    if is_enabled and 'awx' not in settings_module.LOG_AGGREGATOR_LOGGERS:
        awx_logger_instance = None

    add_or_remove_logger('awx.analytics', instance)
    add_or_remove_logger('awx', awx_logger_instance)