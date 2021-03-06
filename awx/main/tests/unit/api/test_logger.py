import pytest

from django.conf import settings

from awx.main.utils.external_logging import construct_rsyslog_conf_template
from awx.main.tests.functional.api.test_settings import _mock_logging_defaults

'''
# Example User Data
data_logstash = {
    "LOG_AGGREGATOR_TYPE": "logstash",
    "LOG_AGGREGATOR_HOST": "localhost",
    "LOG_AGGREGATOR_PORT": 8080,
    "LOG_AGGREGATOR_PROTOCOL": "tcp",
    "LOG_AGGREGATOR_USERNAME": "logger",
    "LOG_AGGREGATOR_PASSWORD": "mcstash"
}

data_netcat = {
    "LOG_AGGREGATOR_TYPE": "other",
    "LOG_AGGREGATOR_HOST": "localhost",
    "LOG_AGGREGATOR_PORT": 9000,
    "LOG_AGGREGATOR_PROTOCOL": "udp",
}

data_loggly = {
    "LOG_AGGREGATOR_TYPE": "loggly",
    "LOG_AGGREGATOR_HOST": "http://logs-01.loggly.com/inputs/1fd38090-2af1-4e1e-8d80-492899da0f71/tag/http/",
    "LOG_AGGREGATOR_PORT": 8080,
    "LOG_AGGREGATOR_PROTOCOL": "https"
}
'''


# Test reconfigure logging settings function
# name this whatever you want
@pytest.mark.parametrize(
    'enabled, type, host, port, protocol, expected_config', [
        (
            True,
            'loggly',
            'http://logs-01.loggly.com/inputs/1fd38090-2af1-4e1e-8d80-492899da0f71/tag/http/',
            None,
            'https',
            '\n'.join([
                'template(name="awx" type="string" string="%rawmsg-after-pri%")\nmodule(load="omhttp")',
                'action(type="omhttp" server="logs-01.loggly.com" serverport="80" usehttps="off" skipverifyhost="off" action.resumeRetryCount="-1" template="awx" errorfile="/var/log/tower/rsyslog.err" healthchecktimeout="5000" restpath="inputs/1fd38090-2af1-4e1e-8d80-492899da0f71/tag/http/")',  # noqa
            ])
        ),
        (
            True,  # localhost w/ custom UDP port
            'other',
            'localhost',
            9000,
            'udp',
            '\n'.join([
                'template(name="awx" type="string" string="%rawmsg-after-pri%")',
                'action(type="omfwd" target="localhost" port="9000" protocol="udp" action.resumeRetryCount="-1" template="awx")',  # noqa
            ])
        ),
        (
            True,  # localhost w/ custom TCP port
            'other',
            'localhost',
            9000,
            'tcp',
            '\n'.join([
                'template(name="awx" type="string" string="%rawmsg-after-pri%")',
                'action(type="omfwd" target="localhost" port="9000" protocol="tcp" action.resumeRetryCount="-1" template="awx")',  # noqa
            ])
        ),
        (
            True,  # https, default port 443
            'splunk',
            'https://yoursplunk/services/collector/event',
            None,
            None,
            '\n'.join([
                'template(name="awx" type="string" string="%rawmsg-after-pri%")\nmodule(load="omhttp")',
                'action(type="omhttp" server="yoursplunk" serverport="443" usehttps="on" skipverifyhost="off" action.resumeRetryCount="-1" template="awx" errorfile="/var/log/tower/rsyslog.err" healthchecktimeout="5000" restpath="services/collector/event")',  # noqa
            ])
        ),
        (
            True,  # http, default port 80
            'splunk',
            'http://yoursplunk/services/collector/event',
            None,
            None,
            '\n'.join([
                'template(name="awx" type="string" string="%rawmsg-after-pri%")\nmodule(load="omhttp")',
                'action(type="omhttp" server="yoursplunk" serverport="80" usehttps="off" skipverifyhost="off" action.resumeRetryCount="-1" template="awx" errorfile="/var/log/tower/rsyslog.err" healthchecktimeout="5000" restpath="services/collector/event")',  # noqa
            ])
        ),
        (
            True,  # https, custom port in URL string
            'splunk',
            'https://yoursplunk:8088/services/collector/event',
            None,
            None,
            '\n'.join([
                'template(name="awx" type="string" string="%rawmsg-after-pri%")\nmodule(load="omhttp")',
                'action(type="omhttp" server="yoursplunk" serverport="8088" usehttps="on" skipverifyhost="off" action.resumeRetryCount="-1" template="awx" errorfile="/var/log/tower/rsyslog.err" healthchecktimeout="5000" restpath="services/collector/event")',  # noqa
            ])
        ),
        (
            True,  # https, custom port explicitly specified
            'splunk',
            'https://yoursplunk/services/collector/event',
            8088,
            None,
            '\n'.join([
                'template(name="awx" type="string" string="%rawmsg-after-pri%")\nmodule(load="omhttp")',
                'action(type="omhttp" server="yoursplunk" serverport="8088" usehttps="on" skipverifyhost="off" action.resumeRetryCount="-1" template="awx" errorfile="/var/log/tower/rsyslog.err" healthchecktimeout="5000" restpath="services/collector/event")',  # noqa
            ])
        ),
        (
            True,  # no scheme specified in URL, default to https, respect custom port
            'splunk',
            'yoursplunk.org/services/collector/event',
            8088,
            'https',
            '\n'.join([
                'template(name="awx" type="string" string="%rawmsg-after-pri%")\nmodule(load="omhttp")',
                'action(type="omhttp" server="yoursplunk.org" serverport="8088" usehttps="on" skipverifyhost="off" action.resumeRetryCount="-1" template="awx" errorfile="/var/log/tower/rsyslog.err" healthchecktimeout="5000" restpath="services/collector/event")',  # noqa
            ])
        ),
        (
            True,  # respect custom http-only port
            'splunk',
            'http://yoursplunk.org/services/collector/event',
            8088,
            None,
            '\n'.join([
                'template(name="awx" type="string" string="%rawmsg-after-pri%")\nmodule(load="omhttp")',
                'action(type="omhttp" server="yoursplunk.org" serverport="8088" usehttps="off" skipverifyhost="off" action.resumeRetryCount="-1" template="awx" errorfile="/var/log/tower/rsyslog.err" healthchecktimeout="5000" restpath="services/collector/event")',  # noqa
            ])
        ),
    ]
)
def test_rsyslog_conf_template(enabled, type, host, port, protocol, expected_config):
    
    mock_settings, _ = _mock_logging_defaults()
    
    # Set test settings
    logging_defaults = getattr(settings, 'LOGGING')
    setattr(mock_settings, 'LOGGING', logging_defaults)
    setattr(mock_settings, 'LOGGING["handlers"]["external_logger"]["address"]', '/var/run/rsyslog/rsyslog.sock')
    setattr(mock_settings, 'LOG_AGGREGATOR_ENABLED', enabled)
    setattr(mock_settings, 'LOG_AGGREGATOR_TYPE', type)
    setattr(mock_settings, 'LOG_AGGREGATOR_HOST', host)
    if port:
        setattr(mock_settings, 'LOG_AGGREGATOR_PORT', port)
    if protocol:
        setattr(mock_settings, 'LOG_AGGREGATOR_PROTOCOL', protocol)

    # create rsyslog conf template 
    tmpl = construct_rsyslog_conf_template(mock_settings)
    
    # check validity of created template
    assert expected_config in tmpl
