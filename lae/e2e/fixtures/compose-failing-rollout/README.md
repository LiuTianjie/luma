# LAE failing rollout fixture

This fixture deliberately preserves the golden application's service, route,
volume, environment, and resource topology while making the public services
unhealthy. validation acceptance uses it to prove that a failed update remains a
failed deployment record and never replaces the application's last healthy
deployment or stable hostnames.
