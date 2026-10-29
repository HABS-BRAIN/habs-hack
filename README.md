# habs

The command-line client for the HABS Neuro-AI Hackathon. Teams do not install
this by hand — their workflow does it, from this branch:

```
pip install "habs-hack[storage] @ git+https://github.com/HABS-BRAIN/habs-hack@main"
```

That is deliberate. Pinning a version would mean editing 35 repositories every
time the client changes; installing from `main` means a fix reaches every team
on their next push.

## What it does

```
habs run      run your process.py on this machine, against the sample data
habs submit   send the committed tree to be built and run on a GPU
habs watch    follow a job somebody else queued (the workflow's second half)
habs status   where your jobs are, and why each one is waiting
habs logs     the whole log of a job, build included — never a tail
habs ls/get   list or download what a job wrote
habs cancel   ask for a running job to be stopped
```

`habs run` needs nothing but Python. Everything else needs either a team
identity (which only that team's GitHub Actions run can present) or the
read-only link HABS sends them.

## What is not here

The scheduler, the dispatcher, the image builder and the Batch backend are not
in this package and never will be. This is a client for one team's own
container; everything that can see every team's work stays on our side.
