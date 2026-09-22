"""Ein Task-Modul fuer den Supervisor im Integrationstest.

Der Supervisor braucht ein ladbares Modul mit mindestens einem Task; was
darin steht, ist fuer den Kontrollkanal gleichgueltig -- geprueft wird die
Zahl der Kindprozesse und ihre Rueckmeldung, nicht ihre Arbeit.
"""

from __future__ import annotations

import os

from swarmq.task import Task, TaskInfo

_QUEUE = os.environ.get("SQD_ITEST_QUEUE", "sqd_ctl_default")


class NoopTask(Task):
    task_info = TaskInfo(name="sqd_ctl_noop", queues=[_QUEUE])

    async def run(self) -> str:
        return "ok"
