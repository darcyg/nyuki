import asyncio
from datetime import datetime
from functools import partial
import logging
from pymongo.errors import AutoReconnect
from tukio import Engine, TaskRegistry, get_broker, EXEC_TOPIC
from tukio.workflow import (
    TemplateGraphError, Workflow, WorkflowTemplate, WorkflowExecState
)

from nyuki import Nyuki
from nyuki.bus import reporting
from nyuki.utils.mongo import MongoManager

from .api.factory import (
    ApiFactoryRegex, ApiFactoryRegexes, ApiFactoryLookup, ApiFactoryLookups,
    ApiFactoryLookupCSV
)
from .api.templates import (
    ApiTasks, ApiTemplates, ApiTemplate, ApiTemplateVersion, ApiTemplateDraft
)
from .api.workflows import (
    ApiWorkflow, ApiWorkflows, ApiWorkflowsHistory, ApiWorkflowHistory,
    ApiWorkflowTriggers, ApiWorkflowTrigger, serialize_wflow_exec
)

from .storage import MongoStorage
from .tasks import *
from .tasks.utils import runtime


log = logging.getLogger(__name__)


class BadRequestError(Exception):
    pass


def sanitize_workflow_exec(obj):
    """
    Replace any object value by 'internal data' string to store in Mongo.
    """
    types = [dict, list, tuple, str, int, float, bool, type(None), datetime]
    if type(obj) not in types:
        obj = 'Internal server data: {}'.format(type(obj))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            obj[key] = sanitize_workflow_exec(value)
    elif isinstance(obj, list):
        for item in obj:
            item = sanitize_workflow_exec(item)
    return obj


class WorkflowInstance:
    """
    Holds a workflow pair of template/instance.
    Allow retrieving a workflow exec state at any moment.
    TODO: These instances should be shared within nodes using a DB
    """
    ALLOWED_EXEC_KEYS = ['requester', 'track']

    def __init__(self, template, instance, org=None, **kwargs):
        self._template = template
        # Set organization attr on the workflow instance to use in tasks.
        instance.organization = org
        self._instance = instance
        self._organization = org
        self._exec = {
            key: kwargs[key]
            for key in kwargs
            if key in self.ALLOWED_EXEC_KEYS
        }

    @property
    def template(self):
        return self._template

    @property
    def instance(self):
        return self._instance

    @property
    def organization(self):
        return self._organization

    @property
    def exec(self):
        return self._exec

    def report(self):
        """
        Merge a workflow exec instance report and its template.
        """
        template = self._template.copy()
        inst = self._instance.report()
        tasks = {task['id']: task for task in template['tasks']}

        inst['exec'].update(self._exec)
        for task in inst['tasks']:
            # Stored template contains more info than tukio's (title...),
            # so we add it to the report.
            tasks[task['id']] = {**tasks[task['id']], **task}

        return {
            **self._template,
            'exec': inst['exec'],
            'tasks': [task for task in tasks.values()]
        }


class WorkflowNyuki(Nyuki):

    """
    Generic workflow nyuki allowing data storage and manipulation
    of tukio's workflows.
    https://github.com/optiflows/tukio
    """

    CONF_SCHEMA = {
        'type': 'object',
        'required': ['mongo'],
        'properties': {
            'mongo': {
                'type': 'object',
                'required': ['host'],
                'properties': {
                    'host': {'type': 'string', 'minLength': 1},
                    'database': {'type': 'string', 'minLength': 1}
                }
            },
            'topics': {
                'type': 'array',
                'items': {'type': 'string', 'minLength': 1}
            }
        }
    }
    HTTP_RESOURCES = Nyuki.HTTP_RESOURCES + [
        ApiTasks,  # /v1/workflows/tasks
        ApiTemplates,  # /v1/workflows/templates
        ApiTemplate,  # /v1/workflows/templates/{uid}
        ApiTemplateDraft,  # /v1/workflows/templates/{uid}/draft
        ApiTemplateVersion,  # /v1/workflows/templates/{uid}/{version}
        ApiWorkflows,  # /v1/workflows
        ApiWorkflow,  # /v1/workflows/{uid}
        ApiWorkflowsHistory,  # /v1/workflows/history
        ApiWorkflowHistory,  # /v1/workflows/history/{uid}
        ApiFactoryRegexes,  # /v1/workflows/regexes
        ApiFactoryRegex,  # /v1/workflows/regexes/{uid}
        ApiFactoryLookups,  # /v1/workflows/lookups
        ApiFactoryLookup,  # /v1/workflows/lookups/{uid}
        ApiFactoryLookupCSV,  # /v1/workflows/lookups/{uid}/csv
        ApiWorkflowTriggers,  # /v1/workflows/triggers
        ApiWorkflowTrigger,  # /v1/workflows/triggers/{tid}
    ]

    DEFAULT_POLICY = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.register_schema(self.CONF_SCHEMA)
        self.migrate_config()
        self.engine = None
        self.storage = None
        # Stores workflow instances with their template data
        self.running_workflows = {}
        # Multi-tenant websocket
        self.ws_clients = None

        self.AVAILABLE_TASKS = {}
        for name, value in TaskRegistry.all().items():
            self.AVAILABLE_TASKS[name] = getattr(value[0], 'SCHEMA', {})

        runtime.bus = self.bus
        runtime.config = self.config
        runtime.workflows = self.running_workflows

    @property
    def mongo_config(self):
        return self.config['mongo']

    @property
    def topics(self):
        return self.config.get('topics', [])

    async def setup(self):
        self.engine = Engine(loop=self.loop)
        asyncio.ensure_future(self.reload_from_storage())
        for topic in self.topics:
            asyncio.ensure_future(self.bus.subscribe(
                topic, self.workflow_event
            ))
        # Enable workflow exec follow-up
        get_broker().register(self.report_workflow, topic=EXEC_TOPIC)
        # Set workflow serializer
        self.websocket.add_ready_handler(self.websocket_ready)
        self.websocket.add_close_handler(self.websocket_close)
        self.websocket.serializer = serialize_wflow_exec
        self.ws_clients = {}

    async def reload(self):
        asyncio.ensure_future(self.reload_from_storage())

    async def teardown(self):
        if self.engine:
            await self.engine.stop()

    async def websocket_ready(self, client):
        """
        Immediately send all instances of workflows to the client.
        """
        org = client.websocket.request_headers.get('X-Surycat-Organization')
        if org not in self.ws_clients:
            self.ws_clients[org] = []
        self.ws_clients[org].append(client)
        return [
            workflow.report()
            for workflow in self.running_workflows.get(org, {}).values()
        ]

    async def websocket_close(self, client):
        """
        Called on websocket closing to clean client list.
        """
        for clients in self.ws_clients.values():
            try:
                clients.remove(client)
            except ValueError:
                pass

    def new_workflow(self, template, instance, org=None, **kwargs):
        """
        Keep in memory a workflow template/instance pair.
        """
        wflow = WorkflowInstance(template, instance, org=org, **kwargs)
        if org not in self.running_workflows:
            self.running_workflows[org] = {}
        self.running_workflows[org][instance.uid] = wflow
        return wflow

    async def report_workflow(self, event):
        """
        Send all worklfow updates to the clients.
        """
        source = event.source.as_dict()
        exec_id = source['workflow_exec_id']
        for org_workflows in self.running_workflows.values():
            if exec_id in org_workflows:
                wflow = org_workflows[exec_id]
                break
        source['workflow_exec_requester'] = wflow.exec.get('requester')

        # Workflow ended, clear it from memory
        if event.data['type'] in [
            WorkflowExecState.end.value,
            WorkflowExecState.error.value
        ]:
            async with self.mongo_manager.db_context(wflow.organization) as storage:
                # Sanitize objects to store the finished workflow instance
                asyncio.ensure_future(storage.instances.insert(
                    sanitize_workflow_exec(wflow.report())
                ))
            del self.running_workflows[wflow.organization][exec_id]

        payload = {
            'type': event.data['type'],
            'data': event.data.get('content') or {},
            'source': source,
            'timestamp': datetime.utcnow().isoformat()
        }

        # Is workflow begin, also send the full template.
        if event.data['type'] == WorkflowExecState.begin.value:
            payload['template'] = wflow.template

        await self.websocket.send(
            self.ws_clients.get(wflow.organization, []), payload
        )

    async def workflow_event(self, efrom, data):
        """
        New bus event received, trigger workflows if needed.
        TODO: On multiple DBs, this is massive requests done on each event.
        Check for something lighter.
        """
        try:
            databases = await self.mongo_manager.list_databases()
        except AutoReconnect as exc:
            log.error('Could not trigger workflows from event (%s)', exc)
            return

        # Retrieve full workflow templates
        templates = {}
        wf_templates = self.engine.selector.select(efrom)

        def template_fetched(org, template_uid, future):
            res = future.result()
            templates[template_uid] = {
                'template': res[0],
                'org': org
            }

        tasks = []
        for name in databases:
            async with self.mongo_manager.db_context(name) as storage:
                for wftmpl in wf_templates:
                    task = asyncio.ensure_future(storage.templates.get(
                        wftmpl.uid, draft=False, with_metadata=True
                    ))
                    task.add_done_callback(
                        partial(template_fetched, name, wftmpl.ui)
                    )
                    tasks.append(task)

        if not tasks:
            return
        await asyncio.wait(tasks)

        # Trigger workflows
        instances = await self.engine.data_received(data, efrom)
        for instance in instances:
            self.new_workflow(
                templates[instance.template.uid]['template'],
                instance,
                org=templates[instance.template.uid]['org']
            )

    async def reload_from_storage(self):
        """
        Check mongo, retrieve and load all templates
        """
        self.mongo_manager = MongoManager(MongoStorage, **self.mongo_config)
        try:
            databases = await self.mongo_manager.list_databases()
        except AutoReconnect as exc:
            log.error('Could not reload workflow templates (%s)', exc)
            return

        for name in databases:
            async with self.mongo_manager.db_context(name) as storage:
                templates = await storage.templates.get_all(
                    full=True,
                    latest=True,
                    with_metadata=False
                )

        # templates = []

        # def template_fetched(future):
        #     res = future.result()
        #     templates.extend(res)

        # tasks = []
        # for name in databases:
        #     async with self.mongo_manager.db_context(name) as storage:
        #         task = asyncio.ensure_future(storage.templates.get_all(
        #             full=True, latest=True, with_metadata=False
        #         ))
        #         task.add_done_callback(lambda f: templates.extend(f.result()))
        #         tasks.append(task)

        # if tasks:
        #     await asyncio.wait(tasks)

        for template in templates:
            try:
                await self.engine.load(WorkflowTemplate.from_dict(template))
            except Exception as exc:
                # Means a bad workflow is in database, report it
                reporting.exception(exc)
