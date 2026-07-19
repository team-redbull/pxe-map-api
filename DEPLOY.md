# Deploying pxe-map-api

Deployment automation for the 5 site PXE VMs (RHEL 9.4, SELinux enforcing)
lives in a **separate** repo: `pxe-map-api-ansible`. This repo has no
deployment code or systemd/SELinux/install logic of its own - just the
application (`app/`) and its tests.

```bash
# checked out as a sibling directory of this repo:
cd ../pxe-map-api-ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook site.yml --ask-vault-pass
```

See that repo's `DEPLOY.md` for the full step-by-step guide: prerequisites,
pointing the inventory at your real VMs, generating and `ansible-vault`
encrypting a token per site, reviewing the bind address, running the
playbook, verifying end-to-end, upgrading, and rotating tokens.

See `README.md` here for the API surface and running/testing this service
locally (no Ansible needed for that). See `SPEC.md` for why the app's
safety checks (MAC canonicalization, atomic writes, SELinux labeling)
exist - that context doesn't change based on how the service is deployed.
