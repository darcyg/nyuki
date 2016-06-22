from .factory import FactoryTask
from .join import JoinTask
from .report import ReportTask
from .sleep import SleepTask


# Generic schema to reference a task ID
TASKID_SCHEMA = {
    'type': 'string',
    'description': 'task_id'
}