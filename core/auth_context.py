from contextvars import ContextVar

# Each validation task keeps its own sport, including spawned refresh tasks.
validation_sport = ContextVar("validation_sport", default=None)
