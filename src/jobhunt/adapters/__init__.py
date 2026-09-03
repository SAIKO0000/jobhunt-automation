from jobhunt.adapters.ashby import AshbyAdapter
from jobhunt.adapters.base import SourceAdapter
from jobhunt.adapters.greenhouse import GreenhouseAdapter
from jobhunt.adapters.himalayas import HimalayasAdapter
from jobhunt.adapters.jobicy import JobicyAdapter
from jobhunt.adapters.lever import LeverAdapter
from jobhunt.adapters.manual import ManualAdapter
from jobhunt.adapters.remoteok import RemoteOkAdapter
from jobhunt.adapters.remotive import RemotiveAdapter
from jobhunt.adapters.wwr import WeWorkRemotelyAdapter
from jobhunt.models import SourceKind


def adapter_registry() -> dict[SourceKind, SourceAdapter]:
    adapters: list[SourceAdapter] = [
        RemoteOkAdapter(),
        WeWorkRemotelyAdapter(),
        HimalayasAdapter(),
        JobicyAdapter(),
        RemotiveAdapter(),
        GreenhouseAdapter(),
        LeverAdapter(),
        AshbyAdapter(),
        ManualAdapter(),
    ]
    return {adapter.source: adapter for adapter in adapters}


__all__ = ["SourceAdapter", "adapter_registry"]
