# First boot (cloud-init)

A NoCloud cidata ISO is built with `community.libvirt.virt_volume`
`create_cidata_cdrom` (Rock Ridge names `user-data` / `meta-data` /
`network-config`). The network config enables DHCP on any guest ethernet
NIC matching `e*` (`enp1s0`, `ens3`, `eth0`, …), so a base image without
default network configuration still requests a libvirt lease even when the
virtio interface name varies by PCI slot. (`virbr0` is the hypervisor
bridge for the libvirt NAT network — it is not the guest NIC.)

Cloud-init then applies:

- Hostname / FQDN (`vm_hostname`.`vm_domain`, default
  `sap-kvm-vm.lab.eng.tlv2.redhat.com`)
- Root password SSH (`ssh_pwauth`, `00-sap.conf` before RHEL
  `50-redhat.conf`, plaintext `chpasswd`)
- Yum repos from `guest_image_compose_url` (`vm_repos`), packages from
  `vm_packages` (includes `libxcrypt-compat` for HCMT) plus
  `qemu-guest-agent`
- Filesystem grow on `/dev/vda`
- SELinux `permissive` (`setenforce 0` equivalent)

Password SSH is required; the role does not inject SSH keys. Recreate with
`-e destroy_existing_vm=true` when user-data changes, because cloud-init
user-data runs on first boot only. `libxcrypt-compat`, guest latency cmdline
(`transparent_hugepage={{ vm_guest_thp }}`, `mitigations=off`,
`intel_idle.max_cstate=1`, `processor.max_cstate=1`), guest `haltpoll`
(`vm_guest_halt_poll_ns`, default `400000`), and SELinux permissive are
also applied over SSH after boot so an existing guest still gets them
(`post_boot.yml` reboots the guest when the latency cmdline is not yet
active).

`post_boot.yml` waits up to `cloud_init_boot_timeout` (default 1800s) for
`/var/lib/cloud/instance/boot-finished` before continuing. cloud-init is
installing real packages from 5 real repos on a working network (see [Guest network](guest-network.md)),
so on a slow mirror this easily exceeds a couple of minutes; a repo that is
unreachable/misconfigured can also make it stall closer to the full timeout
per repo before `skip_if_unavailable` gives up on it.
