"""Packet-level guard check in disposable Linux namespaces, never host rules."""
import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid

from luma.nomad_firewall import network_policy, render_nftables


ENABLED = sys.platform == "linux" and os.geteuid() == 0 and os.environ.get("LUMA_TEST_NETNS") == "1" and shutil.which("ip") and shutil.which("nft")

LISTENER = r'''
import socket,threading,time
def listen(family,kind,port):
 s=socket.socket(family,kind)
 s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
 if family==socket.AF_INET6: s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
 s.bind(('::' if family==socket.AF_INET6 else '0.0.0.0',port))
 if kind==socket.SOCK_STREAM: s.listen()
 while True:
  if kind==socket.SOCK_STREAM:
   conn,_=s.accept();conn.sendall(b'ok');conn.close()
  else:
   _,peer=s.recvfrom(20);s.sendto(b'ok',peer)
for family in (socket.AF_INET,socket.AF_INET6):
 for port in (4646,4647,4648,7890,18080,3306,17890): threading.Thread(target=listen,args=(family,socket.SOCK_STREAM,port),daemon=True).start()
 for port in (4648,3478,49200): threading.Thread(target=listen,args=(family,socket.SOCK_DGRAM,port),daemon=True).start()
while True: time.sleep(1)
'''

PROBE = r'''
import socket,sys
host,port,protocol=sys.argv[1],int(sys.argv[2]),sys.argv[3]
kind=socket.SOCK_DGRAM if protocol=='udp' else socket.SOCK_STREAM
s=socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET,kind);s.settimeout(0.5)
try:
 s.connect((host,port))
 if protocol=='udp': s.send(b'hi')
 assert s.recv(2)==b'ok'
except (OSError,AssertionError): sys.exit(1)
finally: s.close()
'''


@unittest.skipUnless(ENABLED, "requires disposable Linux root namespaces and LUMA_TEST_NETNS=1")
class NomadPacketBoundary(unittest.TestCase):
    def run_command(self, *args, input=None):
        result = subprocess.run(list(args), input=input, text=True, capture_output=True, timeout=15)
        if result.returncode:
            self.fail("namespace command failed: " + " ".join(args) + "\n" + result.stderr)
        return result.stdout

    def ns(self, name, *args, input=None):
        return self.run_command("ip", "netns", "exec", self.names[name], *args, input=input)

    def setUp(self):
        prefix = "ln" + uuid.uuid4().hex[:6]
        self.names = {role: prefix + "-" + role for role in ("node", "mesh", "public", "container")}
        self.processes = []
        def cleanup():
            for process in self.processes:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            for name in self.names.values():
                subprocess.run(["ip", "netns", "del", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        self.addCleanup(cleanup)
        for name in self.names.values():
            self.run_command("ip", "netns", "add", name)
            self.run_command("ip", "-n", name, "link", "set", "lo", "up")
        links = [
            ("mesh", "tailscale0", "100.64.0.1/24", "100.64.0.2/24", "fd7a:115c:a1e0::1/64", "fd7a:115c:a1e0::2/64"),
            ("public", "public0", "192.0.2.1/24", "192.0.2.2/24", "2001:db8:10::1/64", "2001:db8:10::2/64"),
            ("container", "docker0", "172.19.0.1/24", "172.19.0.2/24", "2001:db8:20::1/64", "2001:db8:20::2/64"),
        ]
        for index, (role, interface, node4, peer4, node6, peer6) in enumerate(links):
            left, right = prefix + str(index) + "a", prefix + str(index) + "b"
            self.run_command("ip", "link", "add", left, "type", "veth", "peer", "name", right)
            self.run_command("ip", "link", "set", left, "netns", self.names["node"])
            self.run_command("ip", "link", "set", right, "netns", self.names[role])
            self.ns("node", "ip", "link", "set", left, "name", interface)
            self.ns(role, "ip", "link", "set", right, "name", "eth0")
            for target, device, ipv4, ipv6 in (("node", interface, node4, node6), (role, "eth0", peer4, peer6)):
                self.ns(target, "ip", "addr", "add", ipv4, "dev", device)
                self.ns(target, "ip", "-6", "addr", "add", ipv6, "dev", device, "nodad")
                self.ns(target, "ip", "link", "set", device, "up")
        self.ns("node", "sysctl", "-qw", "net.ipv4.ip_forward=1")
        self.ns("container", "ip", "route", "add", "100.64.0.0/24", "via", "172.19.0.1")
        self.ns("mesh", "ip", "route", "add", "172.19.0.0/24", "via", "100.64.0.1")
        for role in ("node", "mesh"):
            self.processes.append(subprocess.Popen(["ip", "netns", "exec", self.names[role], sys.executable, "-c", LISTENER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        deadline = time.monotonic() + 5
        while not self.probe("mesh", "100.64.0.1", 18080):
            if time.monotonic() > deadline:
                self.fail("namespace listeners failed to start")
            time.sleep(0.05)

    def probe(self, namespace, address, port, protocol="tcp"):
        return subprocess.run(["ip", "netns", "exec", self.names[namespace], sys.executable, "-c", PROBE, address, str(port), protocol], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3).returncode == 0

    def test_dual_stack_source_interface_and_container_forwarding_boundaries(self):
        self.assertTrue(self.probe("public", "192.0.2.1", 4646))
        self.assertTrue(self.probe("public", "2001:db8:10::1", 4646))
        self.assertTrue(self.probe("container", "100.64.0.2", 4646))
        # Parse the UFW interface exceptions without modifying host rules.
        for interface in ("tailscale0", "docker0", "br+", "nomad"):
            self.run_command("ufw", "--dry-run", "insert", "1", "allow", "in", "on", interface, "to", "any", "port", "7890", "proto", "tcp")
        # Model UFW after the guard: its interface exceptions must preserve
        # legitimate RPC/proxy traffic, while later accepts cannot undo drops.
        accepts = '''add table inet docker_test
add chain inet docker_test input { type filter hook input priority 0; policy accept; }
add chain inet docker_test forward { type filter hook forward priority 0; policy accept; }
add rule inet docker_test input iifname "lo" accept
add rule inet docker_test input iifname "tailscale0" tcp dport { 4646, 4647, 4648, 7890 } accept
add rule inet docker_test input iifname "tailscale0" udp dport { 4648, 7890 } accept
add rule inet docker_test input iifname "docker0" tcp dport 7890 accept
add rule inet docker_test input tcp dport { 4646, 4647, 4648, 7890 } drop
add rule inet docker_test input udp dport { 4648, 7890 } drop
'''
        self.ns("node", "nft", "-f", "-", input=accepts)
        policy = network_policy(os_name="linux", address="100.64.0.1", rpc_peers=["100.64.0.2", "fd7a:115c:a1e0::2"], http_peers=[])
        rules = render_nftables(policy)
        self.ns("node", "nft", "--check", "-f", "-", input=rules)
        self.ns("node", "nft", "-f", "-", input=rules)
        for namespace, address, port, protocol in (
            ("public", "192.0.2.1", 4646, "tcp"), ("public", "192.0.2.1", 4647, "tcp"),
            ("public", "2001:db8:10::1", 4646, "tcp"), ("public", "2001:db8:10::1", 4648, "udp"),
            ("mesh", "100.64.0.1", 4646, "tcp"), ("container", "172.19.0.1", 4647, "tcp"),
            ("container", "2001:db8:20::1", 4646, "tcp"), ("container", "100.64.0.2", 4646, "tcp"),
        ):
            with self.subTest(namespace=namespace, address=address, port=port, protocol=protocol):
                self.assertFalse(self.probe(namespace, address, port, protocol))
        for namespace, address, port in (("mesh", "100.64.0.1", 4647), ("mesh", "fd7a:115c:a1e0::1", 4647),
                                         ("container", "172.19.0.1", 7890), ("public", "192.0.2.1", 18080), ("node", "127.0.0.1", 4646)):
            with self.subTest(allowed=(namespace, address, port)):
                self.assertTrue(self.probe(namespace, address, port))
        for port, protocol in ((3306, "tcp"), (17890, "tcp"), (3478, "udp"), (49200, "udp")):
            with self.subTest(business_port=port):
                self.assertTrue(self.probe("public", "192.0.2.1", port, protocol))


if __name__ == "__main__":
    unittest.main()
