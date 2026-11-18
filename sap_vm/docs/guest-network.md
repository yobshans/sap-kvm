# Guest network / DHCP lease and outbound access

Two different interfaces are involved:

- **Host bridge** (usually `virbr0`): discovered from the libvirt network XML
  and used only for host nftables rules below.
- **Guest NIC** (dynamic name: `enp1s0`, `ens3`, `eth0`, …): configured for
  DHCP by cloud-init `network-config` via `match: name: "e*"`, not hardcoded.

`preflight.yml` ensures the libvirt network (`vm_network`, default
`default`) is active, then reads its XML to find the real bridge device
(e.g. `virbr0`) and inserts rules directly into the host’s nftables
`ip filter INPUT` and `ip filter FORWARD` chains:

- `iifname "<bridge>" udp dport { 53, 67 } accept`
- `iifname "<bridge>" tcp dport 53 accept`
- `ct state established,related accept`
- `iifname "<bridge>" accept`

These rules are reapplied and **verified** on every run (the playbook fails
if the bridge still isn’t accepted after insert), so they also recover from
a host reboot or firewall reload. They are not persisted outside Ansible —
a later `firewall-cmd --reload` / iptables restore can wipe them again.

Some hosts have a host firewall with a catch-all `REJECT` at the end of
`INPUT` and/or `FORWARD` that pre-dates the libvirt network, even though
`virsh net-list` shows the network as active:

- **`INPUT`** blocks DHCP/DNS requests before they reach `dnsmasq`. Symptom:
  guest `enp*` is `UP` but has no address, `virsh net-dhcp-leases` is empty,
  and `vm_define.yml`'s `Wait for VM DHCP lease` times out. cloud-init then
  also fails package installs with `Could not resolve host`.
- **`FORWARD`** blocks NAT'd guest traffic to the outside world (yum repos,
  downloads) — this is a separate chain from `INPUT` and can be broken
  independently. Symptom: DHCP and SSH into the guest work fine, `dnf
  install` inside the guest fails immediately with `Could not connect to
  server`, and outbound traffic is rejected or prohibited.

Manual recovery on the hypervisor (then re-request DHCP in the guest):

```bash
bridge=virbr0
nft insert rule ip filter INPUT iifname "$bridge" udp dport '{ 53, 67 }' accept
nft insert rule ip filter INPUT iifname "$bridge" tcp dport 53 accept
nft insert rule ip filter FORWARD ct state established,related accept
nft insert rule ip filter FORWARD iifname "$bridge" accept
# in the guest:
nmcli device reapply enp1s0   # or: dhclient -v enp1s0
```
