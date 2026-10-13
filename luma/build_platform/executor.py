"""One native-platform BuildKit execution inside a resource-bounded Nomad task."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import selectors
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from .model import DIGEST
from .placement import verify_native


class Executor:
    def __init__(self, bootstrap: dict, root: Path):
        self.bootstrap, self.root = bootstrap, root
        self.sequence = 0
        self.pending = []
        self.redactions = [bootstrap['token']]
        self.child = None
        self.stopping = False

    def request(self, route, body=None):
        url = self.bootstrap['endpoint'] + '/v1/executors/' + self.bootstrap['attemptId'] + route
        headers = {'Authorization': 'Bearer ' + self.bootstrap['token'], 'Content-Type': 'application/json'}
        data = json.dumps(body).encode() if body is not None else None
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers, data=data), timeout=10) as response:
            return json.load(response)

    def log(self, message, name='Build image', status='start'):
        for secret in self.redactions:
            if secret:
                message = message.replace(secret, '[redacted]')
        self.pending.append({'name': name, 'status': status, 'message': message[:8192], 'ts': int(time.time())})
        if len(self.pending) >= 24:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        body = {'batchId': f'{self.bootstrap["attemptId"]}-{self.sequence}', 'events': self.pending}
        path = self.root / ('log-' + str(self.sequence) + '.json')
        path.write_text(json.dumps(body))
        # Stable batch IDs and bytes: an accepted response lost in transit is
        # retried without duplicating log entries. Pipe backpressure bounds RAM.
        self.retry('/logs', body)
        path.unlink()
        self.sequence += 1
        self.pending = []

    def retry(self, route, body):
        for attempt in range(6):
            try:
                return self.request(route, body)
            except (OSError, ValueError):
                if self.stopping or attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 5))

    def stop(self, *_):
        self.stopping = True
        if self.child and self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGTERM)
        raise KeyboardInterrupt

    def command(self, argv, env, timeout):
        self.child = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(self.child.stdout, selectors.EVENT_READ)
        deadline, last_flush, buffer = time.monotonic() + timeout, time.monotonic(), b''
        try:
            while selector.get_map():
                if time.monotonic() > deadline:
                    raise TimeoutError('BuildKit execution deadline exceeded')
                for key, _ in selector.select(timeout=1):
                    block = os.read(key.fileobj.fileno(), 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    buffer += block
                    while b'\n' in buffer or len(buffer) >= 8192:
                        end = buffer.find(b'\n')
                        end = min(end, 8192) if end >= 0 else 8192
                        self.log(buffer[:end].decode('utf-8', errors='replace'))
                        buffer = buffer[end + (1 if buffer[end:end+1] == b'\n' else 0):]
                if time.monotonic() - last_flush >= 2:
                    self.flush()
                    last_flush = time.monotonic()
            if buffer:
                self.log(buffer.decode('utf-8', errors='replace'))
            code = self.child.wait(timeout=10)
            self.flush()
            if code:
                raise RuntimeError(f'BuildKit exited with code {code}')
        finally:
            selector.close()
            if self.child.poll() is None:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=10)
            self.child.stdout.close()
            self.child = None

    def run(self):
        lease = self.request('/lease?nodeId=' + os.environ.get('LUMA_EXECUTOR_NODE_ID', ''))
        verify_native(lease['platform'], os.environ.get('LUMA_EXECUTOR_HOST_ARCH', ''))
        auth = lease['registryAuth']
        self.redactions.extend([auth['password'], base64.b64encode(f"{auth['username']}:{auth['password']}".encode()).decode()])
        archive, source = self.root / 'source.tar.gz', self.root / 'source'
        digest = hashlib.sha256()
        with urllib.request.urlopen(lease['sourceUrl'], timeout=60) as response, archive.open('wb') as output:
            received = 0
            while block := response.read(1024 * 1024):
                received += len(block)
                if received > lease['sourceBytes']:
                    raise ValueError('source archive exceeds recorded size')
                output.write(block)
                digest.update(block)
        if digest.hexdigest() != lease['sourceChecksum']:
            raise ValueError('source archive checksum mismatch')
        source.mkdir()
        with tarfile.open(archive) as incoming:
            # The executor image requires Python 3.13. data rejects traversal,
            # devices and links outside the extraction destination.
            incoming.extractall(source, filter='data')
        docker = self.root / 'docker'
        docker.mkdir(mode=0o700)
        config = docker / 'config.json'
        config.write_text(json.dumps({'auths': {lease['registryHost']: {'auth': base64.b64encode(f"{auth['username']}:{auth['password']}".encode()).decode()}}}))
        config.chmod(0o600)
        env = {**os.environ, 'DOCKER_CONFIG': str(docker)}
        if lease.get('proxy'):
            env.update(HTTP_PROXY=lease['proxy'], HTTPS_PROXY=lease['proxy'])
            self.redactions.append(lease['proxy'])
        socket = 'unix://' + str(self.root / 'buildkit.sock')
        toml = self.root / 'buildkit.toml'
        toml.write_text('[worker.oci]\n  gc = true\n  max-parallelism = 2\n  platforms = ["' + lease['platform'] + '"]\n' + (f'\n[registry."{lease["registryHost"]}"]\n  http = true\n' if lease['insecure'] else ''))
        daemon_log = (self.root / 'buildkitd.log').open('wb')
        daemon = subprocess.Popen(['buildkitd', '--addr', socket, '--root', str(self.root / 'cache'), '--config', str(toml)], stdout=daemon_log, stderr=subprocess.STDOUT, env=env)
        try:
            for _ in range(60):
                if daemon.poll() is not None:
                    raise RuntimeError('BuildKit daemon could not start')
                check = subprocess.run(['buildctl', '--addr', socket, 'debug', 'workers'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if check.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('BuildKit daemon readiness timed out')
            images, caches = {}, []
            for target in lease['targets']:
                context = (source / target['context']).resolve()
                dockerfile = (source / target['dockerfile']).resolve()
                context.relative_to(source.resolve())
                dockerfile.relative_to(source.resolve())
                metadata = self.root / ('metadata-' + hashlib.sha256(target['key'].encode()).hexdigest()[:16] + '.json')
                options = ',registry.insecure=true' if lease['insecure'] else ''
                command = ['buildctl', '--addr', socket, 'build', '--progress', 'plain', '--frontend', 'dockerfile.v0',
                           '--local', 'context=' + str(context), '--local', 'dockerfile=' + str(dockerfile.parent),
                           '--opt', 'filename=' + dockerfile.name, '--opt', 'platform=' + lease['platform'],
                           '--output', 'type=image,name=' + target['pushRef'] + ',push=true' + options,
                           '--metadata-file', str(metadata), '--export-cache', 'type=registry,ref=' + target['cacheRef'] + ',mode=max' + options]
                if target.get('target'):
                    command.extend(['--opt', 'target=' + target['target']])
                arguments = dict(target.get('args') or {})
                if lease.get('proxy'):
                    arguments.setdefault('HTTP_PROXY', lease['proxy'])
                    arguments.setdefault('HTTPS_PROXY', lease['proxy'])
                for key, value in arguments.items():
                    command.extend(['--opt', f'build-arg:{key}={value}'])
                for cache in target['cacheImports']:
                    command.extend(['--import-cache', 'type=registry,ref=' + cache + options])
                self.command(command, env, lease['timeout'])
                value = json.loads(metadata.read_text()).get('containerimage.digest', '')
                if not DIGEST.fullmatch(value):
                    raise ValueError('BuildKit did not return an immutable image digest')
                images[target['key']] = target['pushRef'].rsplit(':', 1)[0] + '@' + value
                caches.append(target['cacheRef'])
            self.log('Images pushed; awaiting registry verification', status='ok')
            self.flush()
            self.retry('/receipt', {'platform': lease['platform'], 'images': images, 'cacheRefs': caches})
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=5)
            daemon_log.close()


def main():
    bootstrap = json.loads(Path(os.environ['LUMA_EXECUTOR_BOOTSTRAP']).read_text())
    with tempfile.TemporaryDirectory(prefix='luma-build-', dir=os.environ.get('NOMAD_ALLOC_DIR') or None) as directory:
        executor = Executor(bootstrap, Path(directory))
        signal.signal(signal.SIGTERM, executor.stop)
        signal.signal(signal.SIGINT, executor.stop)
        executor.run()


if __name__ == '__main__':
    main()
