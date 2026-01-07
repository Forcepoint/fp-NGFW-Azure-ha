from dataclasses import dataclass
from typing import Optional


@dataclass
class HAScriptContext:
    """Context info for HA script mainloop.

    This context is mainly used for detecting and acting on change.
    """

    # Last known local admin status ("online"/"offline").
    # Used by both primary and secondary engine.
    prev_local_status: Optional[str] = None

    # Last known primary admin status ("online"/"offline").
    # Only used by secondary engine.
    prev_primary_status: Optional[str] = None

    # True if the local engine was active (that is, route table pointing to
    # self) at the previous iteration.
    prev_local_active: Optional[bool] = None

    # Log info only when something changes.
    display_info_needed: bool = True

    # Number of successive failed probes:
    #  - for primary, used for remote probing
    #  - for secondary, used for primary probing
    probe_fail_count: int = 0
