import time
import asyncio
import logging
from uuid import uuid4
from bson.objectid import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from motor.motor_asyncio import AsyncIOMotorClient


log = logging.getLogger(__name__)


def timed(func):
    async def wraps(*args, **kwargs):
        start = time.time()
        r = await func(*args, **kwargs)
        log.debug("'%s' took %s s", func.__name__, time.time() - start)
        return r
    return wraps


class Migration:

    def __init__(self, host, database, **kwargs):
        client = AsyncIOMotorClient(host, **kwargs)
        self.db = client[database]

    @timed
    async def run(self):
        """
        Run the migrations.
        """
        log.info('Starting migrations')
        collections = await self.db.collection_names()
        if 'metadata' in collections:
            await self._migrate_workflow_metadata()
        if 'templates' in collections:
            await self._migrate_workflow_templates()
        if 'workflow-instances' in collections:
            await self._migrate_workflow_instances()
        if 'task-instances' in collections:
            await self._migrate_task_instances()
        if 'instances' in collections:
            await self._migrate_old_instances()
        log.info('Migration passed')

    @timed
    async def _migrate_workflow_metadata(self):
        count = 0
        col = self.db['workflow_metadata']
        async for metadata in self.db['metadata'].find():
            count += 1
            metadata['workflow_template_id'] = metadata.pop('id')
            await col.replace_one({'_id': metadata['_id']}, metadata, upsert=True)
        await self.db['metadata'].drop()
        log.info('%s workflow metadata migrated', count)

    @timed
    async def _migrate_workflow_templates(self):
        """
        Replace documents with new structure.
        """
        count = 0
        template_state = [None, None]
        sort = [('id', ASCENDING), ('version', DESCENDING)]
        wf_col = self.db['workflow_templates']
        task_col = self.db['task_templates']

        # Sorted on version number to set the proper 'state' values.
        async for template in self.db['templates'].find(None, sort=sort):
            count += 1
            # Ensure we got only one 'draft' if any, one 'active' if any,
            # and the rest 'archived'.
            if template['id'] != template_state[0]:
                template_state = [template['id'], 'active']
            if template['draft'] is True:
                state = 'draft'
            else:
                state = template_state[1]
                if state == 'active':
                    template_state[1] = 'archived'

            template['state'] = state
            template['timeout'] = None
            del template['draft']
            # Remove tasks.
            tasks = template.pop('tasks')
            await wf_col.replace_one(
                {'_id': template['_id']}, template, upsert=True
            )

            # Migrate and insert task templates.
            workflow_template = {
                'id': template['id'],
                'version': template['version'],
            }
            task_bulk = task_col.initialize_unordered_bulk_op()
            for task in tasks:
                task['workflow_template'] = workflow_template
                task = self._migrate_one_task_template(task)
                task_bulk.find({
                    'id': task['id'],
                    'workflow_template.id': workflow_template['id'],
                    'workflow_template.version': workflow_template['version'],
                }).upsert().replace_one(task)
            await task_bulk.execute()

        await self.db['templates'].drop()
        log.info('%s workflow templates splited and migrated', count)

    @timed
    async def _migrate_workflow_instances(self):
        """
        Replace workflow instance documents with new structure.
        """
        i, count = (0,) * 2
        col = self.db['workflow_instances']
        old_col = self.db['workflow-instances']
        bulk = col.initialize_unordered_bulk_op()
        old_bulk = old_col.initialize_unordered_bulk_op()
        async for workflow in old_col.find():
            i += 1
            count += 1
            instance = workflow.pop('exec')
            instance['_id'] = workflow.pop('_id')
            instance['template'] = workflow
            bulk.insert(instance)
            old_bulk.find({'_id': instance['_id']}).remove_one()

            if i == 1000:
                await asyncio.wait([
                    asyncio.ensure_future(bulk.execute()),
                    asyncio.ensure_future(old_bulk.execute()),
                ])
                bulk = col.initialize_unordered_bulk_op()
                old_bulk = old_col.initialize_unordered_bulk_op()
                i = 0

        if i > 0:
            await asyncio.wait([
                asyncio.ensure_future(bulk.execute()),
                asyncio.ensure_future(old_bulk.execute()),
            ])

        await old_col.drop()
        log.info('%s workflow instances migrated', count)

    def _migrate_one_task_template(self, template):
        """
        Do the task configuration migrations.
        Add the new 'timeout' field.
        """
        config = template['config']
        template['timeout'] = None

        if template['name'] == 'join':
            template['timeout'] = config.get('timeout')

        elif template['name'] == 'trigger_workflow':
            template['timeout'] = config.get('timeout')
            template['config'] = {
                'blocking': config.get('await_completion', True),
                'template': {
                    'service': 'twilio' if 'twilio' in config['nyuki_api'] else 'pipeline',
                    'id': config['template'],
                    'draft': config.get('draft', False),
                },
            }

        elif template['name'] in ['call', 'wait_sms', 'wait_email', 'wait_call']:
            if 'blocking' in config:
                template['timeout'] = config['blocking']['timeout']
                template['config']['blocking'] = True

        return template

    def _new_task(self, task, workflow_instance_id=None):
        """
        Migrate a task instance.
        """
        # If task was never executed, fill it with 'not-started'.
        instance = task.pop('exec') or {
            'id': str(uuid4()),
            'status': 'not-started',
            'start': None,
            'end': None,
            'inputs': None,
            'outputs': None,
            'reporting': None,
        }
        if '_id' in task:
            instance['_id'] = task.pop('_id')
        instance['workflow_instance_id'] = workflow_instance_id or task.pop('workflow_exec_id')
        instance['template'] = self._migrate_one_task_template(task)
        return instance

    @timed
    async def _migrate_task_instances(self):
        """
        Replace documents with new structure.
        """
        i, count = (0,) * 2
        col = self.db['task_instances']
        old_col = self.db['task-instances']
        bulk = col.initialize_unordered_bulk_op()
        old_bulk = old_col.initialize_unordered_bulk_op()
        async for task in old_col.find():
            i += 1
            count += 1
            instance = self._new_task(task)
            bulk.insert(instance)
            old_bulk.find({'_id': instance['_id']}).remove_one()

            if i == 500:
                await asyncio.wait([
                    asyncio.ensure_future(bulk.execute()),
                    asyncio.ensure_future(old_bulk.execute()),
                ])
                bulk = col.initialize_unordered_bulk_op()
                old_bulk = old_col.initialize_unordered_bulk_op()
                i = 0

        if i > 0:
            await asyncio.wait([
                asyncio.ensure_future(bulk.execute()),
                asyncio.ensure_future(old_bulk.execute()),
            ])

        await old_col.drop()
        log.info('%s task instances migrated', count)

    @timed
    async def _migrate_old_instances(self):
        """
        Bring back the old 'instances' collection from the dead.
        """
        i, count, task_count = (0,) * 3
        old_col = self.db['instances']
        workflow_col = self.db['workflow_instances']
        task_col = self.db['task_instances']
        old_bulk = old_col.initialize_unordered_bulk_op()
        bulk = workflow_col.initialize_unordered_bulk_op()
        async for workflow in old_col.find():
            i += 1
            count += 1

            tasks = workflow.pop('tasks')
            instance = workflow.pop('exec')
            instance['_id'] = workflow.pop('_id')
            instance['template'] = workflow
            bulk.insert(instance)
            old_bulk.find({'_id': instance['_id']}).remove_one()
            task_count += len(tasks)

            try:
                await task_col.insert_many([
                    self._new_task(task, instance['id'])
                    for task in tasks
                ])
            except DuplicateKeyError:
                pass

            if i == 500:
                await asyncio.wait([
                    asyncio.ensure_future(bulk.execute()),
                    asyncio.ensure_future(old_bulk.execute()),
                ])
                bulk = workflow_col.initialize_unordered_bulk_op()
                old_bulk = old_col.initialize_unordered_bulk_op()
                i = 0

        if i > 0:
            await asyncio.wait([
                asyncio.ensure_future(bulk.execute()),
                asyncio.ensure_future(old_bulk.execute()),
            ])

        await old_col.drop()
        log.info(
            '%s old instances migrated to new format (including %s tasks)',
            count, task_count,
        )


if __name__ == '__main__':
    logging.basicConfig(format='%(message)s', level='DEBUG')
    m = Migration('localhost', 'twilio')
    asyncio.get_event_loop().run_until_complete(m.run())