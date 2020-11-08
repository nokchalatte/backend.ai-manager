import asyncio
import enum
import functools
import json
import logging
from typing import FrozenSet
import sqlalchemy as sa
import trafaret as t
from typing import (
    Any, Final,
    Iterable,
    Tuple,
)

import aiohttp
from aiohttp import web
import aiohttp_cors
import aiojobs
from aiojobs.aiohttp import atomic
from aiotools import aclosing
from async_timeout import timeout as _timeout

from ai.backend.common import validators as tx
from ai.backend.common.logging import BraceStyleAdapter
from ai.backend.common.utils import host_health_check

from . import ManagerStatus
from .auth import superadmin_required
from .exceptions import (
    InstanceNotFound,
    InvalidAPIParameters,
    GenericBadRequest,
    ServerFrozen,
    ServiceUnavailable,
)
from .types import CORSOptions, WebMiddleware
from .utils import check_api_params
from ..manager import __version__
from ..manager.defs import DEFAULT_ROLE
from ..manager.models import (
    agents, AgentStatus,
    kernels, AGENT_RESOURCE_OCCUPYING_KERNEL_STATUSES
)

log = BraceStyleAdapter(logging.getLogger('ai.backend.gateway.manager'))


class SchedulerOps(enum.Enum):
    INCLUDE_AGENTS = 'include-agents'
    EXCLUDE_AGENTS = 'exclude-agents'


def server_status_required(allowed_status: FrozenSet[ManagerStatus]):

    def decorator(handler):

        @functools.wraps(handler)
        async def wrapped(request, *args, **kwargs):
            status = await request.app['config_server'].get_manager_status()
            if status not in allowed_status:
                if status == ManagerStatus.FROZEN:
                    raise ServerFrozen
                msg = f'Server is not in the required status: {allowed_status}'
                raise ServiceUnavailable(msg)
            return (await handler(request, *args, **kwargs))

        return wrapped

    return decorator


READ_ALLOWED: Final = frozenset({ManagerStatus.RUNNING, ManagerStatus.FROZEN})
ALL_ALLOWED: Final = frozenset({ManagerStatus.RUNNING})


class GQLMutationUnfrozenRequiredMiddleware:

    def resolve(self, next, root, info, **args):
        if info.operation.operation == 'mutation' and \
                info.context['manager_status'] == ManagerStatus.FROZEN:
            raise ServerFrozen
        return next(root, info, **args)


async def detect_status_update(app):
    try:
        async with aclosing(app['config_server'].watch_manager_status()) as agen:
            async for ev in agen:
                if ev.event == 'put':
                    app['config_server'].get_manager_status.cache_clear()
                    updated_status = await app['config_server'].get_manager_status()
                    log.debug('Process-{0} detected manager status update: {1}',
                              app['pidx'], updated_status)
    except asyncio.CancelledError:
        pass


@atomic
async def fetch_manager_status(request: web.Request) -> web.Response:
    log.info('MANAGER.FETCH_MANAGER_STATUS ()')
    try:
        status = await request.app['config_server'].get_manager_status()
        etcd_info = await request.app['config_server'].get_manager_nodes_info()
        configs = request.app['config']['manager']

        async with request.app['dbpool'].acquire() as conn, conn.begin():
            query = (sa.select([sa.func.count(kernels.c.id)])
                       .select_from(kernels)
                       .where((kernels.c.cluster_role == DEFAULT_ROLE) &
                              (kernels.c.status.in_(AGENT_RESOURCE_OCCUPYING_KERNEL_STATUSES))))
            active_sessions_num = await conn.scalar(query)

            # TODO: update logic to return information for multiple managers (HA)
            if '' in etcd_info:
                _id = etcd_info['']
            elif etcd_info:
                _id = list(etcd_info.keys())[0]
            else:
                _id = ''
            nodes = [
                {
                    'id': _id,
                    'num_proc': configs['num-proc'],
                    'service_addr': str(configs['service-addr']),
                    'heartbeat_timeout': configs['heartbeat-timeout'],
                    'ssl_enabled': configs['ssl-enabled'],
                    'active_sessions': active_sessions_num,
                    'status': status.value,
                }
            ]
            return web.json_response({
                'nodes': nodes,
                'status': status.value,                  # legacy?
                'active_sessions': active_sessions_num,  # legacy?
            })
    except:
        log.exception('GET_MANAGER_STATUS: exception')
        raise


@atomic
@superadmin_required
@check_api_params(
    t.Dict({
        t.Key('status'): tx.Enum(ManagerStatus, use_name=True),
        t.Key('force_kill', default=False): t.ToBool,
    }))
async def update_manager_status(request: web.Request, params: Any) -> web.Response:
    log.info('MANAGER.UPDATE_MANAGER_STATUS (status:{}, force_kill:{})',
             params['status'], params['force_kill'])
    try:
        params = await request.json()
        status = params['status']
        force_kill = params['force_kill']
    except json.JSONDecodeError:
        raise InvalidAPIParameters(extra_msg='No request body!')
    except (AssertionError, ValueError) as e:
        raise InvalidAPIParameters(extra_msg=str(e.args[0]))

    if force_kill:
        await request.app['registry'].kill_all_sessions()
    await request.app['config_server'].update_manager_status(status)

    return web.Response(status=204)


@atomic
async def get_announcement(request: web.Request) -> web.Response:
    data = await request.app['config_server'].etcd.get('manager/announcement')
    if data is None:
        ret = {'enabled': False, 'message': ''}
    else:
        ret = {'enabled': True, 'message': data}
    return web.json_response(ret)


@atomic
@superadmin_required
@check_api_params(
    t.Dict({
        t.Key('enabled', default='false'): t.ToBool,
        t.Key('message', default=None): t.Null | t.String,
    }))
async def update_announcement(request: web.Request, params: Any) -> web.Response:
    if params['enabled']:
        if not params['message']:
            raise InvalidAPIParameters(extra_msg='Empty message not allowed to enable announcement')
        await request.app['config_server'].etcd.put('manager/announcement', params['message'])
    else:
        await request.app['config_server'].etcd.delete('manager/announcement')
    return web.Response(status=204)


iv_scheduler_ops_args = {
    SchedulerOps.INCLUDE_AGENTS: t.List(t.String),
    SchedulerOps.EXCLUDE_AGENTS: t.List(t.String),
}


@atomic
@superadmin_required
@check_api_params(
    t.Dict({
        t.Key('op'): tx.Enum(SchedulerOps),
        t.Key('args'): t.Any,
    }))
async def perform_scheduler_ops(request: web.Request, params: Any) -> web.Response:
    try:
        args = iv_scheduler_ops_args[params['op']].check(params['args'])
    except t.DataError as e:
        raise InvalidAPIParameters(
            f"Input validation failed for args with {params['op']}",
            extra_data=e.as_dict(),
        )
    if params['op'] in (SchedulerOps.INCLUDE_AGENTS, SchedulerOps.EXCLUDE_AGENTS):
        schedulable = (params['op'] == SchedulerOps.INCLUDE_AGENTS)
        async with request.app['dbpool'].acquire() as conn, conn.begin():
            query = (
                agents.update()
                .values(schedulable=schedulable)
                .where(agents.c.id.in_(args))
            )
            result = await conn.execute(query)
            if result.rowcount < len(args):
                raise InstanceNotFound()
        if schedulable:
            # trigger scheduler
            await request.app['event_dispatcher'].produce_event('do_schedule')
    else:
        raise GenericBadRequest('Unknown scheduler operation')
    return web.Response(status=204)


@superadmin_required
@check_api_params(
    t.Dict({
        tx.MultiKey('agent_ids', default=[]): t.List(t.Null | t.String),
        t.Key('with_all_agents', default=False): t.ToBool,
    }))
async def health_check(request: web.Request, params: Any) -> web.Response:
    """
    Return manager/agent host status.

    If ``agent_ids`` is empty, just return manager host's status.
    If there are ALIVE agents corresponding to ``agent_ids``, their host status
    will be returned as well. If there is no ALIVE agent corresponding to ``agent_ids``,
    the status will be just an empty dict.

    :param with_all_agents: return host information of all ALIVE agents
    :param agent_ids: agent hosts' IDs to query status
    """
    # Circumvent cyclic imports
    from .resource import get_watcher_info
    from .server import LATEST_API_VERSION

    log.info('HEALTH_CHECK (agents:[{}])', ','.join(params['agent_ids']))

    if params['with_all_agents'] and params['agent_ids']:
        raise InvalidAPIParameters(
            extra_msg='either one of with_all_agents or agent_ids should be given'
        )
    if params['with_all_agents']:
        async with request.app['dbpool'].acquire() as conn, conn.begin():
            query = (
                sa.select([agents.c.id])
                .select_from(agents)
                .where(agents.c.status == AgentStatus.ALIVE)
            )
            result = await conn.execute(query)
            agent_ids = []
            async for row in result:
                agent_ids.append(row.id)
            params['agent_ids'] = agent_ids

    # ## Get daemon information
    etcd_info = await request.app['config_server'].get_manager_nodes_info()
    _id = ''
    if '' in etcd_info:
        _id = etcd_info['']
    elif etcd_info:
        _id = list(etcd_info.keys())[0]

    result = {
        'managers': [
            {
                'id': _id,
                'type': 'manager',
                'version': __version__,
                'api_version': LATEST_API_VERSION,
            }
        ],
    }
    result['managers'][0].update(await host_health_check())
    if not params['agent_ids']:
        return web.json_response(result)

    # ## Get agent host information
    async def _agent_health_check(sess: aiohttp.ClientSession, agent_id: str) -> dict:
        none_result: dict = {agent_id: {}}
        watcher_info = await get_watcher_info(request, agent_id)
        if not watcher_info:
            return none_result
        watcher_url = watcher_info['addr'] / 'health'
        with _timeout(10.0):
            headers = {'X-BackendAI-Watcher-Token': watcher_info['token']}
            async with sess.get(watcher_url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    return none_result

    # TODO: support per-watcher ssl context
    connector = aiohttp.TCPConnector()
    async with aiohttp.ClientSession(connector=connector) as sess:
        scheduler = await aiojobs.create_scheduler(limit=4)
        try:
            jobs = await asyncio.gather(*[
                scheduler.spawn(_agent_health_check(sess, aid)) for aid in params['agent_ids']
            ])
            agent_infos = await asyncio.gather(*[job.wait() for job in jobs])
        finally:
            await scheduler.close()

    result['agents'] = []
    for agent_info in agent_infos:
        result['agents'].append(agent_info)
    return web.json_response(result)


async def init(app: web.Application) -> None:
    app['status_watch_task'] = asyncio.create_task(detect_status_update(app))


async def shutdown(app: web.Application) -> None:
    if app['status_watch_task'] is not None:
        app['status_watch_task'].cancel()
        await app['status_watch_task']


def create_app(default_cors_options: CORSOptions) -> Tuple[web.Application, Iterable[WebMiddleware]]:
    app = web.Application()
    app['api_versions'] = (2, 3, 4)
    cors = aiohttp_cors.setup(app, defaults=default_cors_options)
    status_resource = cors.add(app.router.add_resource('/status'))
    cors.add(status_resource.add_route('GET', fetch_manager_status))
    cors.add(status_resource.add_route('PUT', update_manager_status))
    announcement_resource = cors.add(app.router.add_resource('/announcement'))
    cors.add(announcement_resource.add_route('GET', get_announcement))
    cors.add(announcement_resource.add_route('POST', update_announcement))
    cors.add(app.router.add_route('POST', '/scheduler/operation', perform_scheduler_ops))
    cors.add(app.router.add_route('GET', '/health', health_check))
    app.on_startup.append(init)
    app.on_shutdown.append(shutdown)
    return app, []
