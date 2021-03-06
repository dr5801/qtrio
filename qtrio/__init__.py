"""Top-level package for QTrio."""

from ._version import __version__

from ._exceptions import (
    QTrioException,
    NoOutcomesError,
    RegisterEventTypeError,
    ReturnCodeError,
    RunnerTimedOutError,
)

from ._core import (
    enter_emissions_channel,
    open_emissions_nursery,
    Emissions,
    Emission,
    EmissionsNursery,
    Outcomes,
    run,
    Runner,
)

from ._pytest import host
