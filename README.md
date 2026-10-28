# habs-hack

The `habs` command for the [HABS Neuro-AI Hackathon](https://github.com/HABS-BRAIN/habs-hackathon-template)
(Paris-Saclay, 2–30 November 2026).

Teams do not need to read this repository. It exists so that `pip install` has
somewhere to point: the wheel is attached to each release.

```bash
pip install https://github.com/HABS-BRAIN/habs-hack/releases/download/v0.1.0/habs_hack-0.1.0-py3-none-any.whl
```

Your team's repo already installs it in CI. Install it locally too if you want
to check your work before spending one of the team's GPU turns.

## What it does

```
habs run      run process.py on this machine against the sample in your repo
habs submit   queue a run on a GPU
habs status   where your jobs are, and why they are waiting
habs logs     the whole of stdout and stderr, never a tail
habs cancel   stop a job
habs ls       what a job wrote
```

Everything but `habs run` acts on your own team's storage container, and needs
`HABS_TEAM` and `HABS_STORAGE` in the environment — both are set for you as
variables in your team repo.

It holds no credentials. In CI the token comes from the OIDC login step; on a
laptop, from `az login`.

## Scope

This is the client only. The scheduler, the image builder and the compute
backend are not here and are not published — the hackathon platform runs those
itself.

Start at **[the team template](https://github.com/HABS-BRAIN/habs-hackathon-template)**,
not here.
