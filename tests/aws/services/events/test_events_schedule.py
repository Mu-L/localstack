import json
import time
from datetime import timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from localstack.testing.aws.eventbus_utils import trigger_scheduled_rule
from localstack.testing.aws.util import is_aws_cloud
from localstack.testing.pytest import markers
from localstack.testing.snapshots.transformer_utility import TransformerUtility
from localstack.utils.strings import short_uid
from localstack.utils.sync import retry
from tests.aws.services.events.helper_functions import (
    events_time_string_to_timestamp,
    get_cron_expression,
    is_old_provider,
    sqs_collect_messages,
)


class TestScheduleRate:
    @markers.aws.validated
    def test_put_rule_with_schedule_rate(self, events_put_rule, aws_client, snapshot):
        rule_name = f"rule-{short_uid()}"
        snapshot.add_transformer(snapshot.transform.regex(rule_name, "<rule-name>"))

        response = events_put_rule(Name=rule_name, ScheduleExpression="rate(1 minute)")
        snapshot.match("put-rule", response)

        response = aws_client.events.list_rules(NamePrefix=rule_name)
        snapshot.match("list-rules", response)

    @markers.aws.validated
    def tests_put_rule_with_schedule_custom_event_bus(
        self,
        events_create_event_bus,
        aws_client,
        snapshot,
    ):
        bus_name = f"test-bus-{short_uid()}"
        events_create_event_bus(Name=bus_name)

        rule_name = f"test-rule-{short_uid()}"
        with pytest.raises(ClientError) as e:
            aws_client.events.put_rule(
                Name=rule_name, EventBusName=bus_name, ScheduleExpression="rate(1 minute)"
            )
        snapshot.match("put-rule-with-custom-event-bus-error", e.value.response)

    @markers.aws.validated
    @pytest.mark.parametrize(
        "schedule_expression",
        [
            "rate(10 seconds)",
            "rate(10 years)",
            "rate(1 minutes)",
            "rate(1 hours)",
            "rate(1 days)",
            "rate(10 minute)",
            "rate(10 hour)",
            "rate(10 day)",
            "rate()",
            "rate(10)",
            "rate(10 minutess)",
            "rate(foo minutes)",
            "rate(0 minutes)",
            "rate(-10 minutes)",
            "rate(10 MINUTES)",
            "rate( 10 minutes )",
            " rate(10 minutes)",
        ],
    )
    def test_put_rule_with_invalid_schedule_rate(self, schedule_expression, aws_client):
        with pytest.raises(ClientError) as e:
            aws_client.events.put_rule(
                Name=f"rule-{short_uid()}", ScheduleExpression=schedule_expression
            )

        assert e.value.response["Error"] == {
            "Code": "ValidationException",
            "Message": "Parameter ScheduleExpression is not valid.",
        }

    @markers.aws.validated
    @pytest.mark.skip(reason="flakey when comparing 'messages-second' against snapshot")
    def tests_schedule_rate_target_sqs(
        self,
        sqs_as_events_target,
        events_put_rule,
        aws_client,
        snapshot,
    ):
        queue_name = f"test-queue-{short_uid()}"
        queue_url, queue_arn = sqs_as_events_target(queue_name)

        bus_name = "default"
        rule_name = f"test-rule-{short_uid()}"
        events_put_rule(Name=rule_name, EventBusName=bus_name, ScheduleExpression="rate(1 minute)")

        target_id = f"test-target-{short_uid()}"
        aws_client.events.put_targets(
            Rule=rule_name,
            EventBusName=bus_name,
            Targets=[
                {"Id": target_id, "Arn": queue_arn},
            ],
        )  # cleanup is handled by rule fixture

        response = aws_client.events.list_targets_by_rule(Rule=rule_name)
        snapshot.match("list-targets", response)

        time.sleep(60)
        messages_first = sqs_collect_messages(
            aws_client, queue_url, expected_events_count=1, retries=3
        )

        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("MD5OfBody"),
                snapshot.transform.key_value("ReceiptHandle"),
                snapshot.transform.regex(target_id, "<target-id>"),
                snapshot.transform.regex(rule_name, "<rule-name>"),
                snapshot.transform.regex(queue_name, "<queue-name"),
            ]
        )
        snapshot.match("messages-first", messages_first)

        time.sleep(60)
        messages_second = sqs_collect_messages(
            aws_client, queue_url, expected_events_count=1, retries=3
        )
        snapshot.match("messages-second", messages_second)

        # check if the messages are 60 seconds apart
        time_messages_first = events_time_string_to_timestamp(
            json.loads(messages_first[0]["Body"])["time"]
        )
        time_messages_second = events_time_string_to_timestamp(
            json.loads(messages_second[0]["Body"])["time"]
        )
        time_delta = time_messages_second - time_messages_first
        expected_time_delta = timedelta(seconds=60)
        tolerance = timedelta(seconds=5)
        assert expected_time_delta - tolerance <= time_delta <= expected_time_delta + tolerance

    @markers.aws.validated
    def tests_schedule_rate_custom_input_target_sqs(
        self, sqs_as_events_target, events_put_rule, aws_client, snapshot
    ):
        queue_url, queue_arn = sqs_as_events_target()

        bus_name = "default"
        rule_name = f"test-rule-{short_uid()}"
        events_put_rule(Name=rule_name, EventBusName=bus_name, ScheduleExpression="rate(1 minute)")

        target_id = f"target-{short_uid()}"
        aws_client.events.put_targets(
            Rule=rule_name,
            EventBusName=bus_name,
            Targets=[
                {
                    "Id": target_id,
                    "Arn": queue_arn,
                    "Input": json.dumps({"custom-value": "somecustominput"}),
                },
            ],
        )  # cleanup is handled by rule fixture

        response = aws_client.events.list_targets_by_rule(Rule=rule_name)
        snapshot.match("list-targets", response)

        time.sleep(60)
        messages_first = sqs_collect_messages(
            aws_client, queue_url, expected_events_count=1, retries=3
        )

        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("MD5OfBody"),
                snapshot.transform.key_value("ReceiptHandle"),
                snapshot.transform.regex(target_id, "<target-id>"),
                snapshot.transform.regex(queue_arn, "<queue-arn>"),
            ]
        )
        snapshot.match("messages", messages_first)

    @markers.aws.needs_fixing
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            # tokens and IDs cannot be properly transformed
            "$..eventId",
            "$..uploadSequenceToken",
            # FIXME: storedBytes should be implemented
            "$..storedBytes",
        ]
    )
    @pytest.mark.skip(
        reason="This test is flaky is CI, might be race conditions"  # FIXME: investigate and fix
    )
    def test_scheduled_rule_logs(
        self,
        logs_create_log_group,
        events_put_rule,
        add_resource_policy_logs_events_access,
        aws_client,
        snapshot,
    ):
        schedule_expression = "rate(1 minute)"
        rule_name = f"rule-{short_uid()}"
        snapshot.add_transformers_list(
            [
                snapshot.transform.regex(rule_name, "<rule-name>"),
                snapshot.transform.regex(logs_create_log_group, "<log-group-name>"),
            ]
        )
        snapshot.add_transformer(TransformerUtility.logs_api())

        response = aws_client.logs.describe_log_groups(logGroupNamePrefix=logs_create_log_group)
        log_group_arn = response["logGroups"][0]["arn"]

        rule_arn = events_put_rule(Name=rule_name, ScheduleExpression=schedule_expression)[
            "RuleArn"
        ]
        add_resource_policy_logs_events_access(rule_arn, log_group_arn)

        # TODO: add target to test InputTransformer
        aws_client.events.put_targets(
            Rule=rule_name,
            Targets=[
                {"Id": "1", "Arn": log_group_arn},
                {"Id": "2", "Arn": log_group_arn},
            ],
        )

        trigger_scheduled_rule(rule_arn)

        # wait for log stream to be created
        def _get_log_stream():
            result = (
                aws_client.logs.get_paginator("describe_log_streams")
                .paginate(logGroupName=logs_create_log_group)
                .build_full_result()
            )
            assert len(result["logStreams"]) >= 2
            # FIXME: this is a check against a flake in LocalStack
            # sometimes the logStreams are created but not yet populated with events, so the snapshot fails
            # assert that the stream has the events before returning
            assert result["logStreams"][0]["firstEventTimestamp"]
            return result["logStreams"]

        log_streams = retry(_get_log_stream, 60)
        log_streams.sort(key=lambda stream: stream["creationTime"])
        snapshot.match("log-streams", log_streams)

        # collect events from log streams in group
        def _get_events():
            _events = []

            _response = (
                aws_client.logs.get_paginator("filter_log_events")
                .paginate(logGroupName=logs_create_log_group)
                .build_full_result()
            )
            _events.extend(_response["events"])

            if len(_events) < 2:
                raise AssertionError(
                    f"Expected at least two events in log group streams, was {_events}"
                )
            return _events

        events = retry(_get_events, retries=5)

        events.sort(key=lambda event: event["timestamp"])

        snapshot.match("log-events", events)


class TestScheduleCron:
    @markers.aws.validated
    @pytest.mark.parametrize(
        "schedule_cron",
        [
            "cron(0 2 ? * SAT *)",  # Run at 2:00 am every Saturday
            "cron(0 12 * * ? *)",  # Run at 12:00 pm every day
            "cron(5,35 14 * * ? *)",  # Run at 2:05 pm and 2:35 pm every day
            "cron(15 10 ? * 6L 2002-2005)",  # Run at 10:15 am on the last Friday of every month during the years 2002-2005
            "cron(0 2 ? * SAT#3 *)",  # Run at 2:00 am on the third Saturday of every month
            "cron(* * ? * SAT#3 *)",  # Run every minute on the third Saturday of every month
            "cron(0/5 5 ? JAN 1-5 2022)",  # RUN every 5 minutes on the first 5 days of January 2022
            "cron(0 10 * * ? *)",  # Run at 10:00 am every day
            "cron(15 12 * * ? *)",  # Run at 12:15 pm every day
            "cron(0 18 ? * MON-FRI *)",  # Run at 6:00 pm every Monday through Friday
            "cron(0 8 1 * ? *)",  # Run at 8:00 am on the 1st day of every month
            "cron(0/15 * * * ? *)",  # Run every 15 minutes
            "cron(0/10 * ? * MON-FRI *)",  # Run every 10 minutes Monday through Friday
            "cron(0/5 8-17 ? * MON-FRI *)",  # Run every 5 minutes Monday through Friday between 8:00 am and 5:55 pm
            "cron(0/30 20-23 ? * MON-FRI *)",  # Run every 30 minutes between 8:00 pm and 11:59 pm Monday through Friday
            "cron(0/30 0-2 ? * MON-FRI *)",  # Run every 30 minutes between 12:00 am and 2:00 am Monday through Friday
        ],
    )
    def tests_put_rule_with_schedule_cron(
        self, schedule_cron, events_put_rule, aws_client, snapshot
    ):
        rule_name = f"rule-{short_uid()}"
        snapshot.add_transformer(snapshot.transform.regex(rule_name, "<rule-name>"))

        response = events_put_rule(Name=rule_name, ScheduleExpression=schedule_cron)
        snapshot.match("put-rule", response)

        response = aws_client.events.list_rules(NamePrefix=rule_name)
        snapshot.match("list-rules", response)

    @markers.aws.validated
    @pytest.mark.skipif(
        is_old_provider(),
        reason="V1 provider does not properly validate",
    )
    @pytest.mark.parametrize(
        "schedule_cron",
        [
            "cron(0 1 * * * *)",  # you can't specify the Day-of-month and Day-of-week fields in the same cron expression
            "cron(7 20 * * NOT *)",
            "cron(INVALID)",
            "cron(0 dummy ? * MON-FRI *)",
            "cron(71 8 1 * ? *)",
        ],
    )
    def tests_put_rule_with_invalid_schedule_cron(self, schedule_cron, events_put_rule, snapshot):
        rule_name = f"rule-{short_uid()}"

        with pytest.raises(ClientError) as e:
            events_put_rule(Name=rule_name, ScheduleExpression=schedule_cron)
        snapshot.match("invalid-put-rule", e.value.response)

    @markers.aws.validated
    @pytest.mark.skip("Flaky, target time can be 1min off message time")
    def test_schedule_cron_target_sqs(
        self,
        sqs_as_events_target,
        events_put_rule,
        aws_client,
        snapshot,
    ):
        queue_url, queue_arn = sqs_as_events_target()

        schedule_cron, target_datetime = get_cron_expression(
            1
        )  # only next full minut might be to fast for setup must be UTC time zone

        bus_name = "default"
        rule_name = f"test-rule-{short_uid()}"
        events_put_rule(Name=rule_name, EventBusName=bus_name, ScheduleExpression=schedule_cron)

        target_id = f"target-{short_uid()}"
        aws_client.events.put_targets(
            Rule=rule_name,
            EventBusName=bus_name,
            Targets=[
                {"Id": target_id, "Arn": queue_arn},
            ],
        )

        time.sleep(120)  # required to wait for time delta 1 minute starting from next full minute
        messages = sqs_collect_messages(aws_client, queue_url, expected_events_count=1, retries=5)

        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("MD5OfBody"),
                snapshot.transform.key_value("ReceiptHandle"),
                snapshot.transform.regex(rule_name, "<rule-name>"),
            ]
        )
        snapshot.match("message", messages)

        # check if message was delivered at the correct time
        time_message = events_time_string_to_timestamp(
            json.loads(messages[0]["Body"])["time"]
        ).replace(tzinfo=timezone.utc)

        # TODO fix JobScheduler to execute on exact time
        # round datetime to nearest minute
        if time_message.second > 0:
            time_message += timedelta(minutes=1)
            time_message = time_message.replace(second=0, microsecond=0)

        assert time_message == target_datetime

    @markers.aws.validated
    def tests_scheduled_rule_does_not_trigger_on_put_events(
        self, sqs_as_events_target, events_put_rule, aws_client
    ):
        queue_url, queue_arn = sqs_as_events_target()

        bus_name = "default"
        rule_name = f"test-rule-{short_uid()}"
        events_put_rule(
            Name=rule_name, EventBusName=bus_name, ScheduleExpression="rate(10 minutes)"
        )

        target_id = f"target-{short_uid()}"
        aws_client.events.put_targets(
            Rule=rule_name,
            EventBusName=bus_name,
            Targets=[
                {
                    "Id": target_id,
                    "Arn": queue_arn,
                    "Input": json.dumps({"custom-value": "somecustominput"}),
                },
            ],
        )
        test_event = {
            "Source": "core.update-account-command",
            "DetailType": "core.update-account-command",
            "Detail": json.dumps({"command": ["update-account"]}),
        }
        aws_client.events.put_events(Entries=[test_event])

        messages = aws_client.sqs.receive_message(
            QueueUrl=queue_url, WaitTimeSeconds=10 if is_aws_cloud() else 3
        )
        assert not messages.get("Messages")
