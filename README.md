# Dune: Awakening image extraction example

This repository is an example of how to download the Linux self-hosted server
package for Dune: Awakening, extract and validate its Docker images, and copy
them to a container registry. It does not host the images or run a game server.
Each user is responsible for running the process, providing registry access, and
publishing the images to a registry they own or control.

## How releases work

[`images.lock`](images.lock) is the reviewed input for a mirror release. It pins
the Steam app, Linux depot and depot manifest, along with the game, operator and
Postgres image versions. The tool derives the expected image names and tags from
these pins and rejects packages that do not match them. It contains no registry
address or credentials; each user supplies their own destination and
authentication.

To use different upstream versions, review a candidate package and update the
related pins in `images.lock` together. Download and validate the package, then
review the plan before publishing. This example does not provide a shared image
registry or publish images on behalf of users. Publishing tags does not update
Kubernetes deployments.

## Requirements

- Linux amd64
- Python 3.10 or later
- DepotDownloader 3.4.0 and Skopeo 1.13 or later
- About 30 GiB of free disk for a complete package and verification

On Ubuntu 24.04, install the command-line dependencies with:

```sh
sudo sh scripts/install-tools.sh
```

Downloads use anonymous Steam access. Publishing requires push and pull access
to the destination registry. The source package contains proprietary game
assets; keep downloaded packages and caches private and confirm you have the
right to redistribute images before publishing them.

## Local use

Download the pinned package, validate all archives offline, then inspect the
planned destinations:

```sh
python3 dune_mirror.py download
python3 dune_mirror.py validate

export CI_REGISTRY=registry.example.com
export CI_REGISTRY_IMAGE="$CI_REGISTRY/group/project"
python3 dune_mirror.py plan
```

To publish locally, log in to the registry using Docker credentials available
to Skopeo, then run `python3 dune_mirror.py publish`. Verify the result with:

```sh
python3 dune_mirror.py verify --deep
```

Commands accept `--lock`, `--package`, and `--report` options. See
`python3 dune_mirror.py --help` for details. The default package and report
directories are excluded from Git.

## Tests

Run the offline test suite with:

```sh
python3 -m unittest discover -s tests -v -b
```

An optional integration test exercises Skopeo against a disposable TLS registry.
Set `DUNE_TEST_REGISTRY` to that registry's address before running
`tests/test_registry.py`; do not point it at a production registry.

## GitLab CI

For a GitLab pipeline that runs only the tests and image mirror, use a
`.gitlab-ci.yml` like this:

```yaml
stages: [test, mirror]

test:
  stage: test
  image: python:3.12-slim
  script:
    - python3 -m unittest discover -s tests -v -b
    - python3 -m py_compile dune_mirror.py

.mirror-base:
  image: ubuntu:24.04
  before_script:
    - sh scripts/install-tools.sh
  interruptible: false
  timeout: 6h
  resource_group: dune-official-registry
  artifacts:
    when: always
    expire_in: 30 days
    paths: [reports/]

mirror:
  extends: .mirror-base
  stage: mirror
  rules:
    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_COMMIT_REF_PROTECTED == "true" && $CI_PIPELINE_SOURCE == "web"'
    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_COMMIT_REF_PROTECTED == "true" && $CI_PIPELINE_SOURCE == "push"'
      changes: [images.lock]
    - when: never
  cache:
    key:
      files: [images.lock]
      prefix: steam-linux
    paths: [.work/packages/]
    when: always
  script:
    - test -n "$CI_REGISTRY_IMAGE" && test -n "$CI_REGISTRY" && test -n "$CI_REGISTRY_USER" && test -n "$CI_REGISTRY_PASSWORD" || { echo "Enable Container Registry for this GitLab project so CI_REGISTRY variables are available"; exit 1; }
    - python3 dune_mirror.py download
    - python3 dune_mirror.py plan
    - python3 dune_mirror.py publish
    - python3 dune_mirror.py verify --deep
```

Enable the project's Container Registry so GitLab provides the `CI_REGISTRY_*`
variables. The mirror job runs from the web UI on the protected default branch,
or automatically after a push to that branch when `images.lock` changes. The
test job runs in the pipeline without registry credentials.

## License

This project's code is licensed under the [MIT License](LICENSE).
Downloaded Dune: Awakening server packages and container images remain subject
to their respective owners' licenses.
