import asyncio
import json
from pathlib import Path
from typing import Generator

from _pytest.monkeypatch import MonkeyPatch
from aioresponses import aioresponses
import jsonschema
from mock import Mock
import pytest

from rasa import telemetry
import rasa.constants
from rasa.shared.importers.importer import TrainingDataImporter
from tests import utilities
from tests.conftest import DEFAULT_CONFIG_PATH

TELEMETRY_TEST_USER = "083642a3e448423ca652134f00e7fc76"  # just some random static id
TELEMETRY_TEST_KEY = "5640e893c1324090bff26f655456caf3"  # just some random static id
TELEMETRY_EVENTS_JSON = "docs/docs/telemetry/events.json"


@pytest.fixture(autouse=True)
def patch_global_config_path(tmp_path: Path) -> Generator[None, None, None]:
    """Ensure we use a unique config path for each test to avoid tests influencing
    each other."""
    default_location = rasa.constants.GLOBAL_USER_CONFIG_PATH
    rasa.constants.GLOBAL_USER_CONFIG_PATH = str(tmp_path / "global.yml")
    yield
    rasa.constants.GLOBAL_USER_CONFIG_PATH = default_location


async def test_events_schema(monkeypatch: MonkeyPatch):
    # this allows us to patch the printing part used in debug mode to collect the
    # reported events
    monkeypatch.setenv("RASA_TELEMETRY_DEBUG", "true")
    monkeypatch.setenv("RASA_TELEMETRY_ENABLED", "true")

    mock = Mock()
    monkeypatch.setattr(telemetry, "print_telemetry_event", mock)

    with open(TELEMETRY_EVENTS_JSON) as f:
        schemas = json.load(f)["events"]

    initial = asyncio.Task.all_tasks()
    # Generate all known backend telemetry events, and then use events.json to
    # validate their schema.
    training_data = TrainingDataImporter.load_from_config(DEFAULT_CONFIG_PATH)
    async with telemetry.track_model_training(training_data, "rasa"):
        await asyncio.sleep(1)

    await telemetry.track_telemetry_disabled()

    pending = asyncio.Task.all_tasks() - initial
    await asyncio.gather(*pending)

    assert mock.call_count == 3

    for call in mock.call_args_list:
        event = call.args[0]
        # `metrics_id` automatically gets added to all event but is
        # not part of the schema so we need to remove it before validation
        del event["properties"]["metrics_id"]
        jsonschema.validate(
            instance=event["properties"], schema=schemas[event["event"]]
        )


async def _mock_track_internal_exception(*args, **kwargs) -> None:
    raise Exception("Tracking failed")


def test_config_path_empty(monkeypatch: MonkeyPatch):
    # this tests the patch_global_config_path fixture -> makes sure the config
    # is read from a temp file instead of the default location
    assert "/.config/rasa" not in rasa.constants.GLOBAL_USER_CONFIG_PATH


def test_segment_request_header():
    assert telemetry.segment_request_header(TELEMETRY_TEST_KEY) == {
        "Content-Type": "application/json",
        "Authorization": "Basic NTY0MGU4OTNjMTMyNDA5MGJmZjI2ZjY1NTQ1NmNhZjM6",
    }


def test_segment_payload():
    assert telemetry.segment_request_payload(
        TELEMETRY_TEST_USER, "foobar", {"foo": "bar"}, {}
    ) == {
        "userId": TELEMETRY_TEST_USER,
        "event": "foobar",
        "properties": {"foo": "bar"},
        "context": {},
    }


async def test_track_ignore_exception(monkeypatch: MonkeyPatch):
    monkeypatch.setattr(telemetry, "_send_event", _mock_track_internal_exception)

    # If the test finishes without raising any exceptions, then it's successful
    assert await telemetry.track("Test") is None


def test_initialize_telemetry():
    telemetry.initialize_telemetry()
    assert True


def test_initialize_telemetry_with_env_false(monkeypatch: MonkeyPatch):
    monkeypatch.setenv("RASA_TELEMETRY_ENABLED", "false")
    assert telemetry.initialize_telemetry() is False


def test_initialize_telemetry_with_env_true(monkeypatch: MonkeyPatch):
    monkeypatch.setenv("RASA_TELEMETRY_ENABLED", "true")
    assert telemetry.initialize_telemetry() is True


def test_initialize_telemetry_env_overwrites_config(monkeypatch: MonkeyPatch):
    telemetry.toggle_telemetry_reporting(True)
    assert telemetry.initialize_telemetry() is True

    monkeypatch.setenv("RASA_TELEMETRY_ENABLED", "false")
    assert telemetry.initialize_telemetry() is False


def test_initialize_telemetry_prints_info(monkeypatch: MonkeyPatch):
    # Mock actual training
    mock = Mock()
    monkeypatch.setattr(telemetry, "print_telemetry_reporting_info", mock)

    telemetry.initialize_telemetry()

    mock.assert_called_once()


def test_not_in_ci_if_not_in_ci(monkeypatch: MonkeyPatch):
    for env in telemetry.CI_ENVIRONMENT_TELL:
        monkeypatch.delenv(env, raising=False)

    assert not telemetry.in_continuous_integration()


def test_in_ci_if_in_ci(monkeypatch: MonkeyPatch):
    monkeypatch.setenv("CI", True)

    assert telemetry.in_continuous_integration()


def test_with_default_context_fields_contains_package_versions():
    context = telemetry.with_default_context_fields()
    assert "python" in context
    assert context["rasa_open_source"] == rasa.__version__


def test_default_context_fields_overwrite_by_context():
    context = telemetry.with_default_context_fields({"python": "foobar"})
    assert context["python"] == "foobar"


async def test_track_sends_telemetry_id(monkeypatch: MonkeyPatch):
    monkeypatch.setenv("RASA_TELEMETRY_ENABLED", "true")
    telemetry.initialize_telemetry()

    mock = Mock()
    monkeypatch.setattr(telemetry, "_send_event", mock)
    await telemetry.track("foobar", {"foo": "bar"}, {"baz": "foo"})

    assert telemetry.get_telemetry_id() is not None

    mock.assert_called_once()
    call_args = mock.call_args[0]

    assert call_args[0] == telemetry.get_telemetry_id()
    assert call_args[1] == "foobar"
    assert call_args[2]["foo"] == "bar"
    assert call_args[2]["metrics_id"] == telemetry.get_telemetry_id()
    assert call_args[3]["baz"] == "foo"


def test_toggle_telemetry_reporting(monkeypatch: MonkeyPatch):
    # tests that toggling works if there is no config
    telemetry.toggle_telemetry_reporting(True)
    assert telemetry.initialize_telemetry() is True

    telemetry.toggle_telemetry_reporting(False)
    assert telemetry.initialize_telemetry() is False

    # tests that toggling works if config is set to false
    telemetry.toggle_telemetry_reporting(True)
    assert telemetry.initialize_telemetry() is True


async def test_segment_gets_called(monkeypatch: MonkeyPatch):
    monkeypatch.setenv("RASA_TELEMETRY_WRITE_KEY", "foobar")
    monkeypatch.setenv("RASA_TELEMETRY_ENABLED", "true")
    telemetry.initialize_telemetry()

    with aioresponses() as mocked:
        mocked.post(
            "https://api.segment.io/v1/track", payload={},
        )

        await telemetry.track("test event", {"foo": "bar"}, {"foobar": "baz"})

        r = utilities.latest_request(mocked, "POST", "https://api.segment.io/v1/track")

        assert r

        b = utilities.json_of_latest_request(r)

        assert "userId" in b
        assert b["event"] == "test event"
        assert b["properties"].get("foo") == "bar"
        assert b["context"].get("foobar") == "baz"


async def test_segment_does_not_raise_exception_on_failure(monkeypatch: MonkeyPatch):
    monkeypatch.setenv("RASA_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("RASA_TELEMETRY_WRITE_KEY", "foobar")
    telemetry.initialize_telemetry()

    with aioresponses() as mocked:
        mocked.post("https://api.segment.io/v1/track", payload={}, status=505)

        # this call should complete without throwing an exception
        await telemetry.track("test event", {"foo": "bar"}, {"foobar": "baz"})

        r = utilities.latest_request(mocked, "POST", "https://api.segment.io/v1/track")

        assert r


def test_environment_write_key_overwrites_key_file(monkeypatch: MonkeyPatch):
    monkeypatch.setenv("RASA_TELEMETRY_WRITE_KEY", "foobar")
    assert telemetry.telemetry_write_key() == "foobar"


def test_sentry_event_pii_removal():
    # this is an example event taken from sentry (generated by putting a print
    # into `telemetry.strip_sensitive_data_from_sentry_event`)
    event = {
        "level": "error",
        "exception": {
            "values": [
                {
                    "module": None,
                    "type": "Exception",
                    "value": "Some unexpected exception.",
                    "mechanism": {"type": "excepthook", "handled": False},
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "rasa",
                                "abs_path": "/Users/tmbo/Library/Caches/pypoetry/virtualenvs/rasa-U5VQkfdm-py3.6/bin/rasa",
                                "function": "<module>",
                                "module": "__main__",
                                "lineno": 33,
                                "pre_context": [
                                    "globals().setdefault('load_entry_point', importlib_load_entry_point)",
                                    "",
                                    "",
                                    "if __name__ == '__main__':",
                                    "    sys.argv[0] = re.sub(r'(-script\\.pyw?|\\.exe)?$', '', sys.argv[0])",
                                ],
                                "context_line": "    sys.exit(load_entry_point('rasa', 'console_scripts', 'rasa')())",
                                "post_context": [],
                            },
                            {
                                "filename": "rasa/__main__.py",
                                "abs_path": "/Users/tmbo/lastmile/bot-ai/rasa/rasa/__main__.py",
                                "function": "main",
                                "module": "rasa.__main__",
                                "lineno": 113,
                                "pre_context": [
                                    "",
                                    '    if hasattr(cmdline_arguments, "func"):',
                                    "        rasa.utils.io.configure_colored_logging(log_level)",
                                    "        set_log_and_warnings_filters()",
                                    "        rasa.telemetry.initialize_error_reporting()",
                                ],
                                "context_line": "        cmdline_arguments.func(cmdline_arguments)",
                                "post_context": [
                                    '    elif hasattr(cmdline_arguments, "version"):',
                                    "        print_version()",
                                    "    else:",
                                    "        # user has not provided a subcommand, let's print the help",
                                    '        logger.error("No command specified.")',
                                ],
                                "in_app": True,
                            },
                            {
                                "filename": "rasa/cli/train.py",
                                "abs_path": "/Users/tmbo/lastmile/bot-ai/rasa/rasa/cli/train.py",
                                "function": "train",
                                "module": "rasa.cli.train",
                                "lineno": 69,
                                "pre_context": [
                                    "    training_files = [",
                                    '        get_validated_path(f, "data", DEFAULT_DATA_PATH, none_is_valid=True)',
                                    "        for f in args.data",
                                    "    ]",
                                    "",
                                ],
                                "context_line": '    raise Exception("Some unexpected exception.")',
                                "post_context": [
                                    "",
                                    "    return rasa.train(",
                                    "        domain=domain,",
                                    "        config=config,",
                                    "        training_files=training_files,",
                                ],
                                "in_app": True,
                            },
                        ]
                    },
                }
            ]
        },
        "event_id": "73dd4980a5fd498d96fec2ee3ee0cb86",
        "timestamp": "2020-09-14T14:37:14.237740Z",
        "breadcrumbs": {"values": []},
        "release": "rasa-2.0.0a4",
        "environment": "production",
        "server_name": "99ec342261934892aac1784d1ac061c1",
        "sdk": {
            "name": "sentry.python",
            "version": "0.17.5",
            "packages": [{"name": "pypi:sentry-sdk", "version": "0.17.5"}],
            "integrations": ["atexit", "dedupe", "excepthook"],
        },
        "platform": "python",
    }
    stripped = telemetry.strip_sensitive_data_from_sentry_event(event)

    for value in stripped.get("exception", {}).get("values", []):
        for frame in value.get("stacktrace", {}).get("frames", []):
            # make sure absolute path got removed from all stack entries
            assert not frame.get("abs_path")
