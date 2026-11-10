import pytest

from rts_shared.kafka import region_from_bootstrap

BOOT = "b-1-public.realtimemode.90zhlq.c13.kafka.us-west-2.amazonaws.com:9198,b-2-public.x.kafka.us-west-2.amazonaws.com:9198"


def test_region_from_msk_bootstrap():
    assert region_from_bootstrap(BOOT) == "us-west-2"
    with pytest.raises(ValueError):
        region_from_bootstrap("broker.confluent.cloud:9092")
