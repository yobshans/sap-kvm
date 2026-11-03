# Hypervisor host tuning for CPU-pinned benchmarking (SAP PBO/HCMT)

> ⚠️ **This section changes the HYPERVISOR, not just the guest, and can
> reboot it.** `tasks/host_tuning.yml` always runs (there is no opt-out
> flag) and, when the running kernel's cmdline doesn't already carry the
> values the current topology needs, it runs `grubby` and **reboots the
> hypervisor** — taking down **every other VM currently running on that
> host**, not just this one. It is idempotent (a re-run with an
> already-matching cmdline is a no-op, and it only reboots again if the
> topology changes enough to need different values — e.g. a different
> `vm_vcpus` override), but **do not run this role against a shared or
> production hypervisor without accounting for that.**

## CPU isolation + static hugepages + disabled CPU mitigations (`tasks/host_tuning.yml`)

After topology discovery (`vm_cpu_pins`, `hugepages_per_node`,
`hugepage_nodes`) but before `hugepages.yml`'s runtime reservation,
`templates/host_tuning.j2` computes the desired hypervisor kernel cmdline:

- `isolcpus=<range>` / `nohz_full=<range>` / `rcu_nocbs=<range>` — exactly
  the host CPUs `vm_cpu_pins` dedicates to guest vCPUs (the 1:1 pins
  above), collapsed into ranges. The reserved emulator/iothread core is
  deliberately **left out** of this range — it needs to keep receiving
  normal host scheduling/interrupts/timers, so it's never isolated.
- `default_hugepagesz=1G hugepagesz=1G hugepages=<N>` — a static,
  boot-time 1 GiB hugepage reservation sized from
  `hugepages_per_node × hugepage_nodes` (the same total `hugepages.yml`
  already computes/verifies at runtime), more reliable than runtime
  reservation on a host whose memory is already fragmented.
- `mitigations=off` — disables CPU vulnerability mitigations on the
  hypervisor for benchmark throughput.

Example (2 sockets × 40 cores × SMT-2, 1 reserved core/node at CPU 0/40):

```bash
grubby --update-kernel=ALL \
  --remove-args="isolcpus nohz_full rcu_nocbs hugepages default_hugepagesz hugepagesz mitigations" \
  --args="default_hugepagesz=1G hugepagesz=1G hugepages=280 isolcpus=1-39,41-79 nohz_full=1-39,41-79 rcu_nocbs=1-39,41-79 mitigations=off"
```

This is compared against `ansible_cmdline` (the currently **booted**
kernel's `/proc/cmdline`, not just what's configured for the next boot):
if everything already matches, nothing happens. Otherwise, `grubby` runs
(`--remove-args` first so repeated topology changes replace the old
values instead of accumulating duplicates), the hypervisor **reboots**
(`ansible.builtin.reboot`, waits for it to come back), facts are
re-gathered, and the cmdline is checked again — failing loudly if it still
doesn't match (wrong default boot entry, multiple installed kernels,
etc.) rather than silently continuing on a half-tuned host.

**Sets `mitigations=off`** — disabling CPU vulnerability mitigations
(Spectre/MDS/L1TF/etc.) on the hypervisor is intentional for benchmark
throughput, but it is security-relevant and host-wide. Do not run this role
against a shared or production hypervisor unless that exposure is acceptable.

## KVM kernel module tuning (`tasks/kvm_tuning.yml`)

Runs right after host tuning (so it applies to the freshly (re)loaded
module if a reboot just happened). These are global `kvm`/`kvm_intel`
kernel module parameters — like host tuning above, they affect **every**
VM on this hypervisor, not just this one:

| Parameter | Default | Why |
|---|---|---|
| `/sys/module/kvm/parameters/halt_poll_ns` | `vm_kvm_halt_poll_ns` (2000000) | vCPU busy-waits longer before halting, reducing wakeup latency |
| `/sys/module/kvm/parameters/lapic_timer_advance_ns` | `vm_kvm_lapic_timer_advance_ns` (1000) | Compensates for VMEXIT latency on lapic timer delivery |
| `/sys/module/kvm_intel/parameters/ple_gap` | `vm_kvm_ple_gap` (0) | Disables Pause-Loop Exiting — benchmark workloads with spinning locks (e.g. SAP PBO) cause unnecessary VMEXITs under PLE |
| `/sys/module/kvm_intel/parameters/enable_apicv` | `Y` | Enables APICv and posted interrupts on Intel hosts |

Applied live via sysfs (effective immediately, no reboot) and persisted to
`/etc/modprobe.d/kvm-tuning.conf` so the same values come back on the next
module reload/reboot. `ple_gap` is Intel-specific (Pause-Loop Exiting); on
an AMD host (no `kvm_intel` module) that one tunable is skipped — not
failed — with a note in the output. AMD's equivalent Pause Filter tuning
is not currently handled by this role.
