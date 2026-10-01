# Local full-base build environment, 2026-09-30 resumption

This is a local qualification tool environment, not a release or deployment. The production build recipe remains `.github/workflows/base-image.yml` in the exact readiness checkout. No CI policy or permission change is included.

Tool inputs: production `scripts/node_build_inputs.py` image digest, declaration snapshot 20260904T000000Z, rpi-image-gen commit 262d4df5a9f9d4133370465399a7958a7c22cdc7. Normalize the tool image to the snapshot using the production environment-builder policy before installing packages. Record the final Docker image ID, architecture, package inventory, and Git tool revision. A tag is only a convenience.

The initial tool build without snapshot normalization failed on perl-base version skew; retain tool-build.log. tool-build-normalized.log records the corrected attempt.

Do not use historical kernel/cache images as successful current inputs. Reuse requires package/input hash provenance for the workflow's kernel-cache key and rerun kernel_config_check plus final initrd content verification. Otherwise reproduce the current workflow scratch-root path and package manifest. The existing Raspberry Pi archive declaration is not an immutable package snapshot; record exact installed versions and bytes without claiming reproducibility beyond actual evidence.

Final source input must be the exact stable dirty-source snapshot produced by the lifecycle component owner, with source-inputs.json, git-head.txt, and git-state.txt retained. Reverify inventory and component ABI. HEAD identifies the parent revision only; it must never be labeled the content identity of this dirty build. No new commit is needed.

The assembler calls git solely for HEAD commit time. Mount the original Git common directory read-only at /git and set GIT_DIR=/git/worktrees/photo-wall3, GIT_COMMON_DIR=/git, GIT_WORK_TREE=/source. Mount the frozen dirty source read-only at /source. Record/verify HEAD and timestamp before and after assembly. This preserves the production clock-floor computation while using exact dirty code. Do not run scripts.build_node_components --revision HEAD because it archives committed source and loses the changes.

Build flow after lifecycle closure stabilizes:
1. Build bootstrapper.deb from the frozen source; consume exact node-base.deb and node-display.deb from the matching component build.
2. Render packages and sources from that source declaration. Run pinned rpi-image-gen with appliance/rpi_image_gen/photo-wall-base.yaml configuration and explicit workroot/package paths, reproducing workflow overrides.
3. Run workflow squashfs closure, device, origin exclusion, and installed ABI checks.
4. Build current initrd/kernel scratch through declared mmdebstrap and Raspberry Pi package setup, exact hooks, config verification, mkinitramfs, matched kernel/DTB/overlays/EEPROM extraction.
5. Run unchanged scripts/build_netboot_bundle.sh with all workflow inputs including base ABI; do not skip initrd verification.
6. Verify required artifacts/size/contents/checksums; run scripts.node_release_artifacts for explicit node V2 bundle. Preserve component ref/ABI match and source provenance with all artifacts.

Native Linux named-volume scratch is required for chroot ownership/mount fidelity; Dock scratch is for tool recipes/logs and exported artifacts. Never remove existing caches or volumes. Upstream marks container builds unsupported; successful tool installation is not full image qualification. Keep mount smoke, real rootfs assembly, initrd verification, actual PID1 integration, and physical Pi/PXE/display evidence distinct.

## Saved recipe files

The sanitized tool-only recipe is preserved as [Dockerfile text](tool-build-recipe.txt), [Debian sources](debian-sources.txt), [mount probe text](mount-smoke.sh.txt), [probe output](mount-smoke-output.txt), [image identity](tool-image.txt), and [package inventory](tool-packages.tsv). These are review/reproduction inputs, not instructions to execute a privileged probe automatically. Later rootfs recipes and qualification are separate.

## Prepared environment result

Verified immutable image: sha256:03031bc30a32048aa4ae3022bf3d8bd8636ac5cf8639695022761f6aeb23f001 (arm64). Its PATH selects Debian Python, matching apt-installed YAML/debian/jsonschema/packaging modules. Exact package versions are in tool-packages.tsv.

New exclusive named volume: pw-node-base-resume-20260930, label photo-wall.task=node-resume-20260930. No prior volume replaced. mount-smoke.sh passed private tmpfs/proc/sysfs/devpts mounts with CAP_SYS_ADMIN and seccomp=unconfined; --network none, read-only root, only task volume writable. Probe container exited and was removed; follow-up read-only inspection confirmed empty volume. Volume retained intentionally for build. No full image assembled.

Concrete rootfs invocation inside the isolated builder (paths below are the future read-only source/component mounts and new Linux volume; execute only after source stabilization and prerequisite bootstrapper staging):

```sh
export SOURCE_DATE_EPOCH="$(python3 /source/scripts/debian_packages.py epoch)"
python3 /source/scripts/debian_packages.py packages bootstrapper player > /work/device-packages.list
python3 /source/scripts/debian_packages.py sources > /work/debian-sources.list
/opt/rpi-image-gen/rpi-image-gen build -S /source/appliance/rpi_image_gen -c photo-wall-base.yaml -- \
  IGconf_sys_workroot=/work/rootfs \
  IGconf_app_bootstrapper_deb=/work/bootstrapper.deb \
  IGconf_app_node_base_deb=/components/node-base.deb \
  IGconf_app_node_display_deb=/components/node-display.deb \
  IGconf_app_device_packages=/work/device-packages.list \
  IGconf_app_debian_sources=/work/debian-sources.list
```

Bootstrapper staging must also avoid its committed-source build CLI. Import helpers from /source, use closure_for(BOOTSTRAPPER_POLICY, repo=source), exact package_version and stage_tree arguments as production build(), assert_minimal, then run_dpkg_deb. Its version is content-derived. All host support tool source hashes remain those selected by pinned rpi-image-gen; support compilation precedes rootfs automatically.

## Prepared rootfs and acceptance wrappers

The [rootfs staging text](prepare-rootfs.py.txt), [rootfs build wrapper text](build-rootfs.sh.txt), [workflow renderer text](render-workflow-steps.py.txt), [environment text](workflow-environment.sh.txt), and [acceptance plan](acceptance-plan.md) are saved for review. The renderer was syntax-checked in the tool image; this is not execution of rootfs, kernel, initrd or bundle acceptance. Launch still depends on the actual cold lifecycle prerequisite and its separately reviewed container bounds.

## Project dependency preflight checkpoint

The first rootfs wrapper stopped before staging because the production bootstrapper helper imports Pydantic, absent from the initial tool image. The [failure trace](rootfs-preflight-failure.txt) is retained. A derived arm64 builder now imports the real helper modules using the lockfile's non-development, hash-pinned dependencies isolated under `/opt/photo-wall-build-python`; its identity is `sha256:2e6e0f1ecf7946bd10877069898fa9958881e4fd665702f0a8ae66aa89208a9e`.

The [derived tool recipe](project-tool-build-recipe.txt), [hashed requirements](project-build-requirements.txt), [input provenance](build-project-inputs.json), [image identity](tool-image-project.txt), and [updated source-checking wrapper](prepare-rootfs-project.py.txt) preserve this checkpoint separately from the earlier recipe. The recipe used a local parent tag; the local FROM tag was inspected and its parent image ID verified before the build; the provenance records the resolved immutable parent `sha256:03031bc30a32048aa4ae3022bf3d8bd8636ac5cf8639695022761f6aeb23f001`. The tag alone is not a reproducibility claim. The wrapper verifies the frozen source and lock hashes before importing production helpers.

At this checkpoint the Linux scratch volume remained empty, and the full rootfs build was held for the actual lifecycle source repair. These tool results do not assert successful rootfs, initrd, PXE bundle or physical boot qualification.

## Executed full-base recipe checkpoint

The subsequent component-stopfix build completed its production rootfs, kernel, initrd and bundle content checks. The owning dated full-base evidence supplies the exact qualification and limitations. The earlier prepared recipes above remain historical.

Executed support texts: [rootfs wrapper](build-rootfs.sh-executed.txt), [input staging](prepare-rootfs.py-executed.txt), [workflow environment](workflow-environment.sh-executed.txt), [workflow renderer](render-workflow-steps.py-executed.txt), [image-only rootfs recovery](resume-rootfs-image.sh-executed.txt), [kernel staging recovery](resume-kernel-stage.py-executed.txt), [bundle verification recovery](resume-bundle-verification.sh-executed.txt), [artifact export](export-artifacts.py-executed.txt), and [restricted loop-device probe](scoped-loop-probe.py-executed.txt). These are archived text, not automatic permission to execute privileged tooling.

The [artifact inventory](artifact-inventory-stopfix.json), [rootfs verification](rootfs-stopfix-verify.log.txt), [bundle verification](bundle-stopfix-verify.log.txt), [export summary](artifact-export.log.txt), and [actual initrd mount-probe output](initrd-loop0-probe.log.txt) preserve the compact results. Full artifacts and failed/intermediate logs remain at the local Dock checkpoint. A successful constructed bundle and scoped container mount probe do not establish Pi firmware/PXE boot, physical display continuity or release certification.
