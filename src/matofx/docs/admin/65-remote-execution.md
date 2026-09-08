---
title: Running jobs on other servers
audience: admin
capability: null
slug: remote-execution
summary: Register always-on servers and HPC clusters, store their credentials, and the shared-folder requirement for sending jobs off the MATOFX SPDM™ machine.
---

# Running jobs on other servers

By default a tool runs **on the MATOFX SPDM™ machine itself**. You can also send a
tool's jobs to:

- an **always-on server (AOS)** — a persistent Linux (SSH) or Windows (WinRM)
  machine that MATOFX logs into and starts the job on; or
- an **HPC cluster** — a Slurm or Microsoft HPC head node, where the cluster's
  own scheduler queues and runs the job.

This chapter covers registering those hosts and storing their credentials. You
manage both from **Admin → Execution environments**, on the **Always-on
servers** and **HPC clusters** tabs.

## Before you start: the shared folder

Whichever remote you use, **the MATOFX machine and the remote must both see the
same shared folder** (an SMB/UNC share on Windows, or an NFS/SMB mount on Linux).
MATOFX stages each job's files there and reads the job's progress from there; the
remote only runs the launcher. Register that shared path as a **Storage location**
first (see *Where files live*), and make sure the remote's service account can
read and write it. Without a shared folder, remote jobs can't run.

Each job gets its own folder under the share, named after its job number. If a
job is ever submitted whose folder already exists — which can only happen when a
database has been restored or rolled back to a point *behind* the files already
on the share — MATOFX does **not** overwrite the old folder. It moves the stale
copy aside into an `_orphaned/` sub-folder (preserving its contents so you can
recover any outputs) and stages the new job cleanly. If you see folders
accumulating under `_orphaned/`, that's the signal; they are safe to inspect and
delete once you no longer need them.

## Storing a credential

MATOFX never keeps a host's password or SSH key in its database. Instead each
host row carries a **credential reference** (a short name you choose, e.g.
`aos:winbox` or `hpc:cluster1`), and the actual secret lives in an encrypted
store on the MATOFX machine.

Set up the store once, on the server, from the command line:

1. Generate a master key and add it to `matofx.toml`:

   ```
   matofx secrets init
   ```

   Copy the printed key into the `[secrets]` section:

   ```toml
   [secrets]
   backend = "encrypted_file"
   file_path = "secrets.json"
   master_key = "<the key from secrets init>"
   ```

   Keep the master key safe and **back it up** — without it the stored
   credentials can't be recovered.

2. Store each host's secret under the same reference you'll put on the host row:

   ```
   matofx secrets set aos:winbox          # prompts for the value
   matofx secrets list                    # shows the reference names (not the secrets)
   ```

   For a Windows host or MS-HPC cluster this is the service-account password; for
   a Linux SSH host it's the password or the private-key contents.

On the host's row in the admin page, the credential line shows **set ✓** once the
store contains that reference, or **NOT set** with the exact command to run.

## Registering an always-on server

On the **Always-on servers** tab, click **New always-on server** and fill in:

- **Friendly name** — your label for the host (can't be changed later).
- **Hostname / IP** and **Port** — leave the port blank to use the default (22
  for Linux/SSH, 5986 for Windows/WinRM).
- **OS** — Linux or Windows. This sets the connection method automatically
  (Linux → SSH, Windows → WinRM).
- **Username** and **Credential reference**.
- **Max concurrent jobs** — how many jobs MATOFX will run on this host at once.
  An always-on server has no queue of its own, so this cap stops MATOFX from
  overloading it. Optionally also set a **CPU-core** and/or **Memory budget** —
  then MATOFX only starts a job when the host has the cores and memory the tool
  asks for.

Deactivate a host to stop new jobs going to it without removing its history.

## Registering an HPC cluster

On the **HPC clusters** tab, click **New HPC cluster** and fill in the head-node
**hostname**, **port** (blank → 22 Slurm / 5986 MS-HPC), **scheduler** (Slurm or
MS-HPC, which sets SSH or WinRM automatically), **username**, and **credential
reference**. Unlike an always-on server, an HPC cluster runs its **own**
scheduler, so MATOFX simply hands the job off — there's no concurrency cap to set
here.

**Default queue / partition** is optional. Fill it in if the cluster divides its
machines into named queues and you want MATOFX's jobs to go to a particular one;
leave it blank if the cluster has no partitions, or if you'd rather let the
cluster's own default apply. A tool can name its own queue, which wins over this
one when set.

### Charging jobs to a project (Slurm only)

Many Slurm sites track who used the cluster and charge the time to a project. If
yours does, a job that doesn't name a project is either **rejected** at
submission or **recorded against the wrong project** — so MATOFX has to send the
name with every job.

Slurm has two separate ways of naming it, and a site uses one or the other
(occasionally both). Both fields appear on the cluster's form when the scheduler
is Slurm:

- **Slurm account** — sent as `--account`. The more common of the two, used where
  the site maintains an accounting database of projects and who may charge to
  them.
- **Slurm work key** — sent as `--wckey`. A free-form label, used where the site
  wants the runs tagged without maintaining that database.

Leave both blank if the cluster doesn't do accounting; MATOFX then sends neither
flag, exactly as before these fields existed. **Ask the cluster's administrator
which one they use and what value to put in it** — guessing produces the same
rejection as leaving it blank. Once set, the value applies to every job MATOFX
sends to that cluster, and the cluster's card shows it back to you under
*Charged to*.

Microsoft HPC has no equivalent, so the two fields are hidden for an MS-HPC
cluster.

## Windows hosts use encrypted connections only

For Windows always-on servers and Microsoft HPC clusters, MATOFX talks to the
host over **WinRM with HTTPS and certificate validation** — it never sends the
credential over an unencrypted connection. Make sure WinRM-over-HTTPS is enabled
on the host (the default port is **5986**) and that its certificate is trusted by
the MATOFX machine. A self-signed certificate must be **added to the MATOFX
machine's trust store**; MATOFX will not skip validation.

## Pointing a tool at a host

When adding or editing a tool, set its **execution target** to the always-on
server or HPC cluster you registered. From then on, that tool's jobs run there
instead of on the MATOFX machine. Tools that don't set an execution target keep
running locally.
