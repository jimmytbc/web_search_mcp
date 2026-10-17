"""Exercise the opt-in provider through real MCP client/server dispatch.

Only the HTTP wire is replaced: FastMCP's Streamable HTTP client performs
its actual handshake and tool calls against a deterministic JSON-RPC server.
Run with: uv run python -m unittest tests.test_parallel -v
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from providers import build_providers
from providers.parallel import MCP_URL, USER_AGENT, ParallelError
from tools.search_health import run_search_health
from tools.search_web import _select_providers_for_mode, run_search_web
from utils import cache
from utils.config import load_config


class ParallelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'PYTHON_DOTENV_DISABLED': '1', 'BRAVE_API_KEY': '',
            'EXA_API_KEY': '', 'SERPER_API_KEY': '', 'PARALLEL_ENABLED': 'true',
            'SEARCH_TIMEOUT_SECONDS': '2',
        }, clear=True)
        self.env.start()
        cache.clear()
        self.requests = []
        self.cancelled = asyncio.Event()
        self.started = asyncio.Event()
        self.behavior = 'success'
        self.response = {
            'results': [
                {'url': 'https://docs.python.org/3/library/asyncio-task.html',
                 'title': 'Coroutines and Tasks', 'publish_date': '2026-01-02',
                 'excerpts': ['Tasks can be cancelled.', 'Use try/finally.']},
                {'url': 'https://example.org/guide?utm_source=search',
                 'title': None, 'publish_date': None, 'excerpts': ['A second source.']},
            ],
            'warnings': [{'message': 'Example upstream warning'}],
        }

        async def wire(request):
            self.requests.append(request)
            if request.method == 'GET':
                return httpx.Response(405)
            message = json.loads(request.content)
            if 'id' not in message:
                return httpx.Response(202)
            method = message['method']
            if method == 'initialize':
                result = {'protocolVersion': '2025-11-25',
                          'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'parallel-fixture', 'version': '1'}}
            elif method == 'tools/list':
                result = {'tools': [{'name': 'web_search',
                                    'inputSchema': {'type': 'object'}}]}
            elif method == 'tools/call':
                self.started.set()
                if self.behavior == 'wait':
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.cancelled.set()
                if self.behavior == 'network':
                    raise httpx.ConnectError('fixture connection failed', request=request)
                if self.behavior == 'error':
                    result = {'isError': True, 'content': [
                        {'type': 'text', 'text': 'returned HTTP 429: rate limited'}]}
                elif self.behavior == 'invalid':
                    result = {'content': [{'type': 'text', 'text': 'not JSON'}]}
                elif self.behavior == 'text':
                    result = {'content': [{'type': 'text', 'text': json.dumps(self.response)}]}
                else:
                    result = {'content': [{'type': 'text', 'text': json.dumps(self.response)}],
                              'structuredContent': self.response}
            else:
                raise AssertionError(f'Unexpected MCP method {method}')
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'],
                                           'result': result})

        def transport(url, **kwargs):
            def factory(**client_kwargs):
                return httpx.AsyncClient(transport=httpx.MockTransport(wire), **client_kwargs)
            return StreamableHttpTransport(url, httpx_client_factory=factory, **kwargs)

        self.transport = patch('providers.parallel.StreamableHttpTransport', side_effect=transport)
        self.transport.start()

    def tearDown(self):
        self.transport.stop()
        self.env.stop()
        cache.clear()

    def config(self, **overrides):
        from dataclasses import replace
        return replace(load_config(), **overrides)

    async def search(self, query='Python task cancellation', max_results=5, **overrides):
        config = self.config(**overrides)
        return await run_search_web(query, max_results, 'parallel', config,
                                    build_providers(config))

    def tool_calls(self):
        return [json.loads(r.content) for r in self.requests if r.method == 'POST'
                and json.loads(r.content).get('method') == 'tools/call']

    async def test_mcp_dispatch_mapping_cache_and_wire(self):
        import server
        config = self.config()
        with patch.object(server, '_config', config), patch.object(
            server, '_providers', build_providers(config)
        ):
            async with Client(server.mcp) as client:
                result = await client.call_tool('search_web', {
                    'query': 'Python task cancellation', 'max_results': 2, 'mode': 'parallel'})
                output = result.data
        self.assertEqual(output['providers_used'], ['parallel'])
        self.assertEqual(output['search_status'], 'degraded')
        self.assertEqual(len(output['results']), 2)
        first = output['results'][0]
        self.assertEqual(first['title'], 'Coroutines and Tasks')
        self.assertEqual(first['snippet'], 'Tasks can be cancelled. ... Use try/finally.')
        self.assertEqual(first['published_date'], '2026-01-02')
        self.assertEqual(first['providers'], ['parallel'])
        self.assertEqual(first['domain'], 'docs.python.org')
        self.assertIn('parallel: Example upstream warning', output['warnings'])
        calls = self.tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['params']['name'], 'web_search')
        arguments = calls[0]['params']['arguments']
        self.assertEqual(arguments['objective'], 'Python task cancellation')
        self.assertEqual(arguments['search_queries'], ['Python task cancellation'])
        self.assertGreaterEqual(len(arguments['session_id']), 32)
        self.assertNotIn('model_name', arguments)
        for request in self.requests:
            self.assertEqual(str(request.url), MCP_URL)
            self.assertEqual(request.headers['user-agent'], USER_AGENT)
            self.assertNotIn('authorization', request.headers)
        cached = await self.search(max_results=2)
        self.assertEqual(cached, output)
        self.assertEqual(len(self.tool_calls()), 1)

    async def test_default_and_existing_modes_unchanged(self):
        with patch.dict(os.environ, {'PARALLEL_ENABLED': ''}):
            config = load_config()
        self.assertFalse(config.parallel_enabled)
        self.assertEqual(build_providers(config), [])
        output = await run_search_web('test', 5, 'parallel', config, [])
        self.assertEqual(output['search_status'], 'failed')
        self.assertIn('PARALLEL_ENABLED=true', output['warnings'][0])
        health = await run_search_health(self.config(parallel_enabled=False,
                                                     search_health_dry_run=True), [])
        self.assertEqual(set(health['modes']), {'balanced', 'recall', 'precision'})
        self.assertEqual([p['name'] for p in health['providers']], ['brave', 'exa', 'serper'])
        providers = build_providers(self.config())
        for mode in ['balanced', 'recall', 'precision']:
            selected, warnings = _select_providers_for_mode(mode, providers)
            self.assertEqual(selected, [])
            self.assertFalse(any('parallel' in w for w in warnings))
        self.assertEqual(self.requests, [])

    async def test_opt_in_config_and_health(self):
        for value in ['false', '0', 'invalid']:
            with patch.dict(os.environ, {'PARALLEL_ENABLED': value}):
                self.assertFalse(load_config().parallel_enabled)
        config = self.config(search_health_dry_run=True)
        providers = build_providers(config)
        self.assertEqual([p.name for p in providers], ['parallel'])
        dry = await run_search_health(config, providers)
        self.assertTrue(dry['modes']['parallel']['available'])
        self.assertEqual(dry['providers'][-1]['last_status'], 'not probed')
        self.assertEqual(self.requests, [])
        live = await run_search_health(self.config(), providers)
        self.assertTrue(live['providers'][-1]['reachable'])
        self.assertEqual(len(self.tool_calls()), 1)

    async def test_result_limit_text_fallback_and_session_reuse(self):
        self.behavior = 'text'
        output = await self.search(max_results=1)
        self.assertEqual(len(output['results']), 1)
        await self.search(query='another query')
        self.assertEqual(self.tool_calls()[0]['params']['arguments']['session_id'],
                         self.tool_calls()[1]['params']['arguments']['session_id'])

    async def test_tool_error_and_health_rate_limit(self):
        self.behavior = 'error'
        output = await self.search()
        self.assertEqual(output['search_status'], 'failed')
        self.assertIn('rate limited', output['warnings'][0])
        config = self.config()
        health = await run_search_health(config, build_providers(config))
        self.assertEqual(health['providers'][-1]['last_status'], 'HTTP 429')
        self.assertTrue(health['providers'][-1]['reachable'])

    async def test_malformed_response(self):
        self.behavior = 'invalid'
        output = await self.search()
        self.assertEqual(output['search_status'], 'failed')
        self.assertIn('non-JSON', output['warnings'][0])
        self.behavior = 'success'
        self.response = {'unexpected': []}
        with self.assertRaisesRegex(ParallelError, 'invalid search response'):
            await build_providers(self.config())[0].search('another query', 5)

    async def test_bad_items_skipped_and_empty_results(self):
        self.response['results'].insert(0, {'url': 'javascript:alert(1)', 'excerpts': []})
        output = await self.search()
        self.assertEqual(len(output['results']), 2)
        self.assertIn('parallel skipped a result with invalid fields', output['warnings'])
        self.response = {'results': []}
        output = await self.search(query='nothing matches')
        self.assertEqual(output['search_status'], 'failed')
        self.assertEqual(output['results'], [])

    async def test_network_failure(self):
        self.behavior = 'network'
        output = await self.search()
        self.assertEqual(output['search_status'], 'failed')
        self.assertTrue(output['warnings'])

    async def test_timeout_cancels_inflight_request(self):
        self.behavior = 'wait'
        output = await self.search(search_timeout_seconds=0.2)
        self.assertEqual(output['search_status'], 'failed')
        self.assertIn('timed out', output['warnings'][0])
        self.assertTrue(self.cancelled.is_set())

    async def test_caller_cancellation_propagates(self):
        self.behavior = 'wait'
        task = asyncio.create_task(self.search())
        await asyncio.wait_for(self.started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.cancelled.is_set())


if __name__ == '__main__':
    unittest.main()
