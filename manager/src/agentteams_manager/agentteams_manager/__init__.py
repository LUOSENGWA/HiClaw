"""AgentTeams Manager runtime modules.

Manager-owned configuration bridge, file sync, project/task storage and
the four Manager tools (projectflow / taskflow / message / filesync),
extracted from ``copaw_worker`` so that the QwenPaw Manager image no
longer depends on the legacy CoPaw worker package.

Window note: ``bridge`` and ``sync`` are also consumed by the legacy
CoPaw worker (``copaw_worker``); copies remain there until the CoPaw
runtime is removed. Keep both in sync until that happens.
"""

__version__ = "1.0.0"
