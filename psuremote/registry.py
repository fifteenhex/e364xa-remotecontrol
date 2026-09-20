"""Which driver to use for which instrument."""

from __future__ import annotations

from .a661xc import A661xC
from .e364xa import E364xA
from .instrument import ModelSpec, PowerSupply
from .scpi import Link, LinkError

DRIVERS: tuple[type[PowerSupply], ...] = (E364xA, A661xC)


def all_models() -> dict[str, tuple[type[PowerSupply], ModelSpec]]:
    models = {}
    for driver in DRIVERS:
        for name, spec in driver.models.items():
            models[name] = (driver, spec)
    return models


def lookup_model(name: str) -> ModelSpec:
    """Find a ModelSpec by model number, case-insensitively."""
    return lookup_driver(name)[1]


def lookup_driver(name: str) -> tuple[type[PowerSupply], ModelSpec]:
    wanted = name.strip().upper()
    models = all_models()
    if wanted in models:
        return models[wanted]
    known = ", ".join(sorted(models))
    raise ValueError(f"unknown model {name!r}; supported models are: {known}")


def model_from_idn(idn: str) -> tuple[type[PowerSupply], ModelSpec] | None:
    """Pick a driver from an *IDN? response.

    Both families answer `<manufacturer>,<model>,<serial>,<revision>`, but be
    generous: match any comma-separated field against the known models so an
    unexpected field order still works.
    """
    models = all_models()
    for field in idn.split(","):
        candidate = field.strip().upper()
        if candidate in models:
            return models[candidate]
    return None


def connect(link: Link, model: str | None = None) -> PowerSupply:
    """Open the link and return a driver bound to it.

    With no `model`, the instrument is asked what it is.
    """
    link.open()

    if model is not None:
        driver_class, spec = lookup_driver(model)
        supply = driver_class(link, spec)
        supply.identify()
        return supply

    try:
        idn = link.query("*IDN?")
    except Exception as exc:
        link.close()
        raise LinkError(
            f"could not read *IDN? from {link.description}: {exc}. "
            "Pass --model to skip detection."
        ) from exc

    match = model_from_idn(idn)
    if match is None:
        link.close()
        known = ", ".join(sorted(all_models()))
        raise LinkError(
            f"{link.description} identified as {idn!r}, which is not a "
            f"supported model. Supported: {known}. Pass --model to force one."
        )

    driver_class, spec = match
    supply = driver_class(link, spec)
    supply._idn = idn
    return supply
