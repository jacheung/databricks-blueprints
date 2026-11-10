"""Kafka (Amazon MSK, IAM auth) helpers shared by kafka_stream, the feature_views freshness probes, and the app.

Auth uses a Unity Catalog service credential: Spark's Kafka sink takes it directly
(`databricks.serviceCredential`); Python clients mint MSK IAM tokens from its temporary AWS credentials.
"""

from __future__ import annotations

import os
import re


def region_from_bootstrap(bootstrap_servers: str) -> str:
    match = re.search(r"\.([a-z]{2}-[a-z]+-\d)\.amazonaws\.com", bootstrap_servers)
    if not match:
        raise ValueError(f"not an Amazon MSK bootstrap address: {bootstrap_servers}")
    return match.group(1)


def spark_sink_options(cfg: dict) -> dict:
    """Options for DataFrame.write.format("kafka"). Don't add security.protocol / sasl.mechanism: they
    conflict with the service credential. Longer timeouts ride out brief stalls on public MSK brokers."""
    k = cfg["kafka"]
    return {"kafka.bootstrap.servers": k["bootstrap_servers"], "databricks.serviceCredential": k["service_credential"],
            "topic": k["topic"], "kafka.request.timeout.ms": "60000", "kafka.delivery.timeout.ms": "300000",
            "kafka.max.block.ms": "300000", "kafka.retries": "20"}


def _load_temp_credentials(workspace_client, credential: str, region: str) -> None:
    response = workspace_client.api_client.do(
        "POST", "/api/2.1/unity-catalog/temporary-service-credentials", body={"credential_name": credential})
    creds = response["aws_temp_credentials"]
    os.environ.update(AWS_ACCESS_KEY_ID=creds["access_key_id"], AWS_SECRET_ACCESS_KEY=creds["secret_access_key"],
                      AWS_SESSION_TOKEN=creds["session_token"], AWS_REGION=region)


def client_config(cfg: dict, workspace_client) -> dict:
    """confluent-kafka config for MSK IAM (OAUTHBEARER); refreshes temporary credentials on every token."""
    from aws_msk_iam_sasl_signer import MSKAuthTokenProvider

    k = cfg["kafka"]
    region = region_from_bootstrap(k["bootstrap_servers"])

    def oauth_cb(_config):
        _load_temp_credentials(workspace_client, k["service_credential"], region)
        token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(region)
        return token, expiry_ms / 1000.0

    return {"bootstrap.servers": k["bootstrap_servers"], "security.protocol": "SASL_SSL",
            "sasl.mechanism": "OAUTHBEARER", "oauth_cb": oauth_cb}


def producer(cfg: dict, workspace_client):
    from confluent_kafka import Producer

    # linger.ms=0: send each event immediately; freshness matters more than batching here.
    return Producer({**client_config(cfg, workspace_client), "linger.ms": 0, "acks": "all"})


def ensure_topic(cfg: dict, workspace_client) -> bool:
    """Create the topic if missing (managed MSK disables auto-create). Returns True if created."""
    from confluent_kafka.admin import AdminClient, NewTopic

    k = cfg["kafka"]
    admin = AdminClient(client_config(cfg, workspace_client))
    if k["topic"] in admin.list_topics(timeout=30).topics:
        return False
    for future in admin.create_topics([NewTopic(k["topic"], num_partitions=k["partitions"],
                                                replication_factor=3)]).values():
        future.result(timeout=60)
    return True
