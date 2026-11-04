import test from 'node:test';
import assert from 'node:assert/strict';
import { observationQuery, investigationPath, logsExpression, grafanaRange } from '../../dashboard-src/src/observeContext.ts';
test('fixed context survives switching views and serializes the same Grafana range',()=>{
 const search='observe_since=100&observe_until=200&observe_service=worker&observeTrace='+'a'.repeat(32);
 const next=investigationPath('/apps/orders/logs',search,{logView:'traces'});
 const query=observationQuery(next.split('?')[1],300);
 assert.equal(query.since,100);assert.equal(query.until,200);assert.equal(query.service,'worker');
 assert.deepEqual(grafanaRange(search),{from:'100000',to:'200000'});
 assert.match(next,/observeTrace=/);
});
test('log filters are quoted, and invalid trace IDs never become query expressions',()=>{
 const expr=logsExpression('orders','observe_service='+encodeURIComponent('a" OR *')+'&observeTrace=bad');
 assert.match(expr,/service_name:="a\\" OR \*"/);assert.ok(!expr.includes('trace_id'));
});
