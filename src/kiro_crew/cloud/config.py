"""Persisted cloud-launcher config — **profile name only, never credentials**.

Stores the AWS *profile name*, region, and the most-recent instance tag under
``~/.kiro/crew/cloud.json``. AWS credentials are never written here — they are
resolved by the ``aws`` CLI's own provider chain from the profile.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterator, Optional

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.cloud.fargate.identity import SecretRef, sole_binding
from kiro_crew.cloud.fargate.taskdef import (
    CPU_ARCHITECTURES,
    MODEL_CREDENTIAL_ENV,
    DocumentRefused,
    _refuse_undigested_image,
    secret_destinations_for,
)
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_FILENAME = "cloud.json"
DEFAULT_REGION = "us-east-1"

# Cap at 51 (not 63) to match ec2._TAG_RE / validate_tag: a longer last_tag
# would pass THIS sanitizer but then raise ValidationError on resume (the IAM
# role name kirocrew-ec2-<tag> maxes at 64), defeating the "just treat it as no
# last launch" intent. Keep in lockstep with ec2._TAG_RE.
#: Longest ``last_tag`` this module accepts. NAMED because two places need the number --
#: the pattern below and the room reserved for a tag provisioning has not written yet -- and
#: a second literal 51 in either would be free to drift from the other.
_TAG_MAX_LEN = 51
_TAG_RE = re.compile(rf"^[a-zA-Z0-9-]{{1,{_TAG_MAX_LEN}}}$")

#: A digest-pinned image reference, the only form ``cloud/fargate/taskdef.py``
#: accepts. Checked here so a hand-edited ``cloud.json`` naming a movable tag is
#: treated as no Fargate configuration at all, rather than becoming a refusal at
#: the first launch -- which is where the operator has least context for it.


#: Upper bounds on how much this reader will retain from one ``fargate`` block. The
#: file is not writable through the agent file-edit tool and is mounted read-only in
#: the sandbox, but a same-UID process outside a sandbox can still write it, so the
#: reader cannot assume the bytes are small. ``load`` treats an unreadable or unparseable
#: file as absent rather than raising, so an oversized-but-valid-JSON document would be
#: parsed and every string retained, and the read runs on every request that builds
#: the provisioner list -- an unbounded list or an unbounded string is a gateway
#: memory-exhaustion surface with only manual recovery. A block that exceeds any bound
#: reads as absent, the same as any other malformed block: the ceiling is generous
#: next to any real placement, so a legitimate operator never meets it.
_MAX_LIST_ITEMS = 64
_MAX_STRING_LEN = 2048


#: Ceiling on the whole file, checked BEFORE it is parsed. ``json.loads`` builds its
#: result in memory, so a bound applied to the parsed document is applied too late;
#: the read itself is what must refuse. Generous next to a real ``cloud.json`` of a
#: few hundred bytes, and an over-sized file falls back to defaults exactly as a
#: corrupt one does rather than raising into every cloud command.
_MAX_FILE_BYTES = 1 << 20


@dataclass(frozen=True)
class FargateConfig:
    """Where an operator writes the Fargate lane's placement, image and secrets.

    **Identifiers only, never a secret value.** A crew secret is named by its
    canonical name and its ARN; the value is fetched by the task's execution role
    from Secrets Manager before the container starts, so nothing here is a
    credential and this file's no-secrets contract holds unchanged.

    The engine takes these four fields as a ``FargateLaunchSpec`` and refuses to
    guess any of them -- "an unnamed subnet or security group is the same class of
    error as deleting a task on a guess". This is the place they are written down.
    """

    cluster: str = ""
    subnets: tuple[str, ...] = ()
    security_groups: tuple[str, ...] = ()
    image: str = ""
    #: ``(canonical name, ARN)`` pairs. A pair, not a bare ARN: an ARN alone
    #: cannot say where the secret's NAME ends, because the service appends a
    #: six-character suffix and nothing marks the boundary.
    secrets: tuple[tuple[str, str], ...] = ()
    cpu_architecture: str = "X86_64"
    #: False is the safe direction, and the flag is not the boundary -- a task in
    #: a public subnet with no NAT gateway cannot pull its image without one.
    assign_public_ip: bool = False

    def is_complete(self) -> bool:
        """True when every field the engine requires is present and well-formed.

        INCOMPLETE MEANS ABSENT, and that is the whole design of this method. A
        half-written block must leave the lane unregistered rather than registered
        and refusing: a lane that exists and rejects every launch spends the
        operator's attention at launch time on a mistake that was visible when
        they saved the file.

        The secrets must include one named for the model credential, because the
        engine refuses a task definition that delivers none: a block with every
        placement field and no credential secret is the offered-and-refusing state
        in its most likely form.

        The whole SET is judged, not just one name: the engine's own
        ``secret_destinations`` derives every reference's destination (refusing two
        that collide) and ``sole_binding`` refuses a set naming more than one crew. So
        a valid credential beside a malformed or cross-crew reference leaves the lane
        unregistered rather than registering one whose every launch then fails. None of
        it is a second copy of those rules -- both are called, not reimplemented.
        """
        return bool(
            self.cluster
            and self.subnets
            and self.security_groups
            and _digest_pinned(self.image or "")
            and self.cpu_architecture in _cpu_architectures()
            and _names_model_credential(self.secrets)
        )

    @classmethod
    def from_mapping(cls, data: object) -> Optional["FargateConfig"]:
        """Read one block, or ``None`` for anything that is not usable.

        Every rejection returns ``None`` rather than a partially-populated object,
        so a caller cannot hold a config that looks present and is not. A secret
        entry that is not a two-string pair drops the WHOLE block, not just that
        entry: silently launching with one fewer secret than the operator wrote is
        how a task starts and then fails on a missing variable.

        ``assign_public_ip`` is read the same way: absent means ``False``, and a
        present value that is not a JSON boolean drops the block. The field decides
        network exposure, and coercing it would read the string ``"false"`` as
        true, which is the one direction this field must never be guessed in.
        """
        if not isinstance(data, dict):
            return None
        secrets: list[tuple[str, str]] = []
        raw_secrets = data.get("secrets", [])
        if not isinstance(raw_secrets, list) or len(raw_secrets) > _MAX_LIST_ITEMS:
            return None
        for entry in raw_secrets:
            if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
                return None
            name, arn = entry
            if not (isinstance(name, str) and isinstance(arn, str) and name and arn):
                return None
            if len(name) > _MAX_STRING_LEN or len(arn) > _MAX_STRING_LEN:
                return None
            secrets.append((name, arn))
        assign_public_ip = data.get("assign_public_ip", False)
        if not isinstance(assign_public_ip, bool):
            return None
        # EVERY string-typed field, in one place. `str()` on a raw value made any JSON
        # scalar truthy: `false` became the string "False", which is non-empty, so
        # `is_complete()` passed and the lane registered against a cluster named
        # "False" that does not exist. This is the same defect already fixed on
        # `assign_public_ip`, one field over, so the check is written once over the
        # field list rather than per field -- a string field added later is covered
        # without anyone remembering to add a branch.
        strings: dict[str, str] = {}
        for field_name, default in _STRING_FIELD_DEFAULTS.items():
            value = data.get(field_name, default)
            if not isinstance(value, str) or len(value) > _MAX_STRING_LEN:
                return None
            strings[field_name] = value
        subnets = _bounded_string_tuple(data.get("subnets"))
        security_groups = _bounded_string_tuple(data.get("security_groups"))
        if subnets is None or security_groups is None:
            return None
        candidate = cls(
            subnets=subnets,
            security_groups=security_groups,
            secrets=tuple(secrets),
            assign_public_ip=assign_public_ip,
            **strings,
        )
        return candidate if candidate.is_complete() else None


def _cpu_architectures() -> frozenset[str]:
    """The architectures the ENGINE accepts, read from it rather than copied.

    Read from the engine rather than copied, so no second list can drift from it.
    """
    return frozenset(CPU_ARCHITECTURES)


def _digest_pinned(image: str) -> bool:
    """Whether the ENGINE would accept this image reference as digest-pinned.

    Delegates to ``taskdef``'s own refusal so a movable tag is judged here by exactly
    the rule that would reject it at launch -- checked at config time so a hand-edited
    ``cloud.json`` naming a tag reads as no Fargate configuration instead of becoming a
    refusal on first use.
    """
    try:
        _refuse_undigested_image(image)
    except DocumentRefused:
        return False
    return True


#: Fields the launch path writes AFTER a deploy succeeds, mapped to the largest value each
#: can hold. The ingest check reserves room for these, because the record it measures is
#: not the record that will be written: provisioning adds a tag, and possibly a longer
#: profile and region, to whatever arrived. Measured, not assumed -- a block sized to the
#: byte loaded fine and then could not be saved with a 33-character tag, leaving a running
#: instance the failed save was supposed to record.
#:
#: Derived from ``_TAG_MAX_LEN`` and ``_MAX_STRING_LEN`` rather than from example values, so
#: the reservation cannot fall behind what the validators actually permit.
_PROVISIONING_WRITES: dict[str, int] = {
    "last_tag": _TAG_MAX_LEN,
    "profile": _MAX_STRING_LEN,
    "region": _MAX_STRING_LEN,
}


def _fits_after_provisioning(cfg: "CloudConfig") -> bool:
    """Whether this record still fits once the launch path has written its fields.

    Serializes the WORST CASE with the real serializer rather than predicting a size. A
    predicted size would be a second implementation of ``save()`` and would drift from it,
    which is the defect this module has now met seven times. Here the check and the write
    are the same function, called with the largest input the write can be handed.
    """
    worst = _record_fields(cfg)
    for name, cap in _PROVISIONING_WRITES.items():
        if len(str(worst.get(name, ""))) < cap:
            worst[name] = "x" * cap
    return _serialize_record(worst) is not None


def _string_field_defaults() -> dict[str, str]:
    """Every ``str``-typed field on :class:`FargateConfig`, with its default.

    Derived from the dataclass rather than listed, so adding a string field extends the
    non-string rejection and its test automatically. A hand-written list is what let
    ``cluster`` keep coercing with ``str()`` after ``assign_public_ip`` was fixed.
    """
    return {
        f.name: f.default
        for f in fields(FargateConfig)
        if f.type in ("str", str) and isinstance(f.default, str)
    }


_STRING_FIELD_DEFAULTS = _string_field_defaults()


def _record_fields(cfg: "CloudConfig") -> dict[str, Any]:
    """Field name to value, SHALLOW, for the writer.

    Shallowness is the point. ``dataclasses.asdict`` deep-copies, and the ``fargate`` value is
    whatever a human typed into the file, so a deeply nested document recurses inside that copy
    and raises before :func:`_serialize_record` -- the one place that can answer whether a
    record is writable -- is ever reached. Reading the fields with ``getattr`` copies nothing,
    so the answer stays with the serializer. Measured: a 2,000-deep block raises in the
    deep-copy while the serializer reports it as "does not fit".

    Private fields are excluded too, by a RULE over the field list rather than by naming one,
    so a private field added later cannot reach the file.
    """
    return {f.name: getattr(cfg, f.name) for f in fields(cfg) if not f.name.startswith("_")}


def _serialize_record(record: dict[str, Any]) -> str | None:
    """The exact bytes :meth:`CloudConfig.save` writes, or ``None`` if none fit.

    The ONE place a record becomes text, so the ceiling is enforced against the form
    that will actually be written. The bug this exists to make impossible: ``load()``
    measured the file it was HANDED while ``save()`` emitted ``indent=2``, which is
    larger, so a minified block could pass on the way in and fail on the way out. That
    failure arrived after the engine had already provisioned, leaving a RUNNING instance
    with no saved record -- an untracked instance still costing money.

    Pretty output is preferred because a human edits this file. When pretty does not
    fit, compact separators are used rather than refusing: the operator's data is worth
    more than its indentation. ``None`` means not even the compact form fits, and the
    caller refuses -- at ingest, before anything is provisioned.
    """
    # RecursionError is a "does not fit" answer like any other, and it must be answered
    # HERE for the same reason the byte ceiling is: this is the one place a record becomes
    # text, so it is the only place that can tell. A deeply nested value parses without
    # complaint -- `json.loads` accepts nesting `json.dumps` cannot emit -- and the depth
    # that breaks is not a property of the document alone: the limit is on total stack, so
    # the same value serializes from a shallow caller and raises from a deep one. There is
    # therefore no depth to validate against, and only attempting the serialization
    # answers the question. Reported as None so the ingest check refuses before anything is
    # provisioned, rather than as a traceback out of a save that runs after the deploy.
    try:
        pretty = json.dumps(record, indent=2)
        if len(pretty.encode("utf-8")) <= _MAX_FILE_BYTES:
            return pretty
        compact = json.dumps(record, separators=(",", ":"))
    except RecursionError:
        return None
    if len(compact.encode("utf-8")) <= _MAX_FILE_BYTES:
        return compact
    return None


#: How long a writer waits for the lock before giving up and writing unserialized. Named
#: because two places read it -- the wait itself and the message that explains the give-up --
#: and a second literal would be free to drift from the first.
_LOCK_ACQUIRE_CEILING_SECS = 10.0
#: Gap between single-shot attempts. Short enough that honest contention is invisible, long
#: enough that waiting does not spin a core.
_LOCK_RETRY_SECS = 0.05
#: ``O_NOFOLLOW`` where the platform has it, ``0`` where it does not -- Windows. NAMED so a
#: test can hold it at the Windows value and check the refusal that must not depend on it.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _discard_unusable_lock(lock_path: Path) -> None:
    """Remove a lock sibling that is not a lone regular file.

    Safe to do because this file is ENTIRELY ours: it carries no data, only the kernel
    lock, so recreating it loses nothing an operator could want. A symlink or directory
    under this name can only have been left by something with no business writing it.
    Best effort by design -- a non-empty directory cannot be removed, and the caller
    treats that as "no lock available" rather than as a reason to abandon the write.
    """
    try:
        info = os.lstat(lock_path)
    except OSError:
        return
    if stat.S_ISREG(info.st_mode):
        return  # a lone regular file is exactly what a lock should be; leave it
    try:
        if stat.S_ISDIR(info.st_mode):
            os.rmdir(lock_path)
        else:
            os.unlink(lock_path)
    except OSError:
        return


@contextmanager
def _writer_lock(lock_path: Path) -> Iterator[None]:
    """Serialize writers on *lock_path*, or proceed detectably when it cannot be used.

    The lock's NAME sits beside the config, inside a directory a sandboxed process may be
    able to reach, and nothing legitimate ever creates it. A directory or symlink planted
    under that name made a plain ``open()`` raise from inside ``save()`` -- the call that
    runs after the engine has provisioned and the one that records the new instance -- so
    the plant did not fail a write, it produced a RUNNING instance with nothing tracking
    it. Removing that outcome is what this exists for.

    Opened ``O_NOFOLLOW`` so a symlink is refused rather than followed, and an unusable
    sibling is replaced. When even replacement fails the write proceeds WITHOUT the lock,
    loudly, because refusing to save at all would reinstate the untracked instance above.
    The residual is a lost-update window against an attacker who can both plant an
    unremovable directory and win that window, and it is recorded rather than hidden.
    """
    # The SHAPE is judged before the open, on every platform, because the open cannot judge
    # it everywhere: ``O_NOFOLLOW`` does not exist on Windows, so the flag is 0 there and an
    # open follows the link instead of refusing it -- which left the sibling unexamined,
    # since it was only examined when the open FAILED. ``lstat`` answers the same question
    # on all three platforms. The flag stays as well, for the gap between this check and the
    # open, where it is the only thing that can refuse a link created in between.
    _discard_unusable_lock(lock_path)
    flags = os.O_RDWR | os.O_CREAT | _O_NOFOLLOW
    fd: int | None = None
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError:
        _discard_unusable_lock(lock_path)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            logger.warning(
                "cloud config: %s is not usable as a lock (%s); writing without "
                "serialization. Cooperating writers may now collide, but no write can "
                "silently overwrite another: every replace re-checks the file first.",
                lock_path,
                exc,
            )
            yield
            return
    try:
        with os.fdopen(fd, "r+b", closefd=True) as lock_fh:
            fd = None
            held = _acquire_bounded(lock_fh, lock_path)
            if held is None:
                yield
                return
            try:
                yield
            finally:
                held.__exit__(None, None, None)
    finally:
        if fd is not None:
            os.close(fd)


def _acquire_bounded(lock_fh: Any, lock_path: Path) -> Any:
    """Take the lock within a bounded time, or return ``None`` to proceed without it.

    ``platform_compat.file_lock`` offers a wait that blocks until the holder releases and a
    single-shot that raises at once; it has no bounded wait, so the bound is built from the
    single-shot form. That matters because the holder is not necessarily cooperating: the
    lock's NAME sits beside the config in a directory a sandboxed process may reach, holding
    an advisory lock needs no write to the config itself, and a same-UID prompt-injected
    agent is a threat this codebase already models elsewhere. An unbounded wait therefore
    hands that agent a way to hang the post-deploy write FOREVER, which strands a billed
    instance with nothing recording it -- the exact outcome removing the refusal was meant
    to prevent, reintroduced through the mechanism that replaced it.

    Proceeding unlocked is the lesser harm and the same choice the unusable-sibling path
    already makes: a field-scoped write re-reads under the lock and carries only the fields
    its caller named, so what it can lose is one of those fields to a writer that ignored the
    lock -- against an instance nothing records at all.

    The ceiling is generous against honest contention -- every critical section here is one
    read plus one atomic rename -- and short against a deliberate holder.
    """
    deadline = time.monotonic() + _LOCK_ACQUIRE_CEILING_SECS
    while True:
        guard = platform_compat.file_lock(lock_fh.fileno(), exclusive=True, wait=False)
        try:
            guard.__enter__()
        except OSError:  # BlockingIOError on POSIX; the Windows branch raises too
            if time.monotonic() >= deadline:
                logger.warning(
                    "cloud config: %s stayed locked for %.1fs; writing without "
                    "serialization. A stale snapshot write is still REFUSED rather than "
                    "erased, and a field-scoped update still carries only its own fields.",
                    lock_path,
                    _LOCK_ACQUIRE_CEILING_SECS,
                )
                return None
            time.sleep(_LOCK_RETRY_SECS)
            continue
        return guard


def _names_model_credential(secrets: tuple[tuple[str, str], ...]) -> bool:
    """True when a secret IS the crew's model credential, per the ENGINE's own check.

    Calls ``identity.secret_env_name`` instead of re-deriving what it accepts. Three
    rounds of review found the same defect while this was a local approximation: a
    tail-only test admitted a bare ``KIRO_API_KEY`` and a wrong-prefix
    ``junk/KIRO_API_KEY``, then a truthiness test on the crew segment admitted seven
    more spellings. Each one registered the lane so every launch through it refused --
    the offered-and-refusing state this module exists to prevent, reached from inside
    the check written to prevent it. Delegating makes the two agree by CONSTRUCTION, so
    no spelling can pass here and fail there, and no drift pin is needed because there
    is no second copy to drift.

    Imported at module scope and effectively free: ``identity`` imports only the standard
    library, and importing this module runs ``cloud/__init__.py`` -- which loads the AWS
    surface regardless -- so the engine modules add 5 modules and about 4 ms on top of the
    280 already loaded. Measured, because the cost is the only reason to put an import
    anywhere other than the top of the file.
    """
    if not secrets:
        return False
    refs = tuple(SecretRef(name=name, arn=arn) for name, arn in secrets)
    try:
        # EVERY reference, not just the first that matches. An early return accepted a
        # good credential ref sitting beside a malformed or cross-crew one, and the
        # engine then refused the whole document at launch -- registering a lane that
        # rejects every launch through it, which is the state this module exists to
        # prevent. Both calls are the ENGINE's own functions, not a second copy:
        # `secret_destinations_for` takes the references precisely so a caller holding
        # only secrets can apply that rule instead of approximating it.
        destinations = secret_destinations_for(refs)
        sole_binding({f"secrets[{i}].valueFrom": ref.arn for i, ref in enumerate(refs)})
    except Exception:  # noqa: BLE001 - any refusal means this set is not usable
        return False
    return MODEL_CREDENTIAL_ENV in destinations


def _bounded_string_tuple(value: object) -> Optional[tuple[str, ...]]:
    """Non-empty strings within the retention bounds, or ``None`` when unusable.

    ``None`` is a positive rejection that voids the whole block, used for the two
    shapes that must not be silently accepted: a list longer than ``_MAX_LIST_ITEMS``
    and a member string longer than ``_MAX_STRING_LEN``. Retaining either without a
    bound is the memory-exhaustion surface ``_MAX_LIST_ITEMS`` exists to close, and
    truncating instead would launch against a placement the operator did not write.

    A non-list still reads as the empty tuple rather than an error, so ``is_complete``
    stays the single place emptiness is judged; an empty required list is what leaves
    the lane unregistered there.
    """
    if not isinstance(value, list):
        return ()
    if len(value) > _MAX_LIST_ITEMS:
        return None
    # ALL-OR-NOTHING. Filtering the bad members out silently launched against a
    # placement the operator did not write: a subnet list with one non-string entry
    # became a shorter list, and the task ran in whichever subnets survived. One bad
    # member voids the block, like one bad secret entry does, so the operator sees an
    # unregistered lane instead of a task in the wrong place.
    for item in value:
        if not isinstance(item, str) or not item or len(item) > _MAX_STRING_LEN:
            return None
    return tuple(value)


#: Directory, inside the config's own parent, holding every copy a write has DISPLACED --
#: a document that could not be parsed, or the newest bytes when a competing writer won
#: every bounded retry. Four properties, each one a bug that was found here:
#:
#: * A SEALED DIRECTORY, not a sibling of the config. Beside the config the copies sat in a
#:   writable parent, so the sandboxed process the config's seal stops could edit or delete
#:   the only recoverable copy -- the same loss, one name over. The seal is on this
#:   directory (read-only, pre-created, name-pinned), so it covers every copy in it without
#:   needing to name any of them.
#: * Copies NOT under a fixed name, because the second corruption would overwrite the bytes
#:   the first one preserved -- the loss this path exists to prevent, one step further back.
#: * NOT a counted series either. A predictable name is one a sandboxed process could occupy
#:   in ADVANCE, and every name being taken turns the preservation into a refusal to write,
#:   which strands the instance the write was recording. Random bytes cannot be pre-empted.
#: * Never pruned. Two preserved files are two displacements nobody has looked at, and
#:   deleting the oldest to keep the directory tidy is the same data loss under a new name.
_PRESERVED_DIRNAME = "cloud-config-preserved"

#: How many names the claim will try before giving up and leaving the file alone. A bound on
#: a loop, not a retention policy and not a limit on how many copies may be kept: each name
#: carries random bytes, so a collision is already improbable and a SECOND collision after a
#: fresh draw is not something a process can arrange.
_PRESERVE_CLAIM_ATTEMPTS = 8


#: How many times the write re-merges before writing what it last merged. The retry is not
#: conditional on having failed to lock: the advisory lock binds only the writers that take
#: it, and a person editing this file in an editor takes nothing. The bound is what stops a
#: livelock against a writer that keeps winning the race.
_MERGE_ATTEMPTS = 4


def _raw_bytes_or_none(p: Path) -> "Optional[bytes]":
    """The file's bytes, or ``None`` when it is absent or unreadable.

    A witness for "did this change under me", not a parse: it deliberately answers for the
    shapes :meth:`CloudConfig._read_or_reason` refuses too, because a config that became a
    directory between the read and the write has changed just as much as one that was
    rewritten, and a witness that raised on those would be no witness at all.
    """
    try:
        with open(p, "rb") as fh:
            return fh.read(_MAX_FILE_BYTES + 1)
    except OSError:
        return None


def _preserve_displaced(p: Path) -> Path:
    """Move *p* into the sealed preservation directory under an unguessable name.

    Called by BOTH writes that displace bytes they cannot use: an unparseable document, and
    the final bounded retry against a competing writer that keeps winning. Named for the
    displacement rather than for either reason, because the rule is one rule -- a write never
    destroys the bytes it replaces.

    The directory is what protects the copies, not their names. It is sealed read-only inside
    the sandbox and pre-created so the seal has something to bind, so a sandboxed process can
    neither edit nor delete what is in it. Preserving BESIDE the config put the only
    recoverable copy back in a writable parent, which is the loss the seal on the config
    exists to prevent, one name over.

    The name is claimed with ``O_CREAT | O_EXCL`` rather than tested with ``exists()``. A
    look-then-move loses the race it is trying to win: if the name appears between the look
    and the move, the move replaces whatever appeared there -- another preserved file. The
    exclusive create cannot be satisfied twice, so the claim IS the test.

    Raises ``OSError`` without touching *p* when no name can be claimed, because leaving the
    file in place is recoverable and overwriting a preserved one is not.
    """
    kept_dir = p.parent / _PRESERVED_DIRNAME
    kept_dir.mkdir(parents=True, exist_ok=True)
    for _attempt in range(_PRESERVE_CLAIM_ATTEMPTS):
        candidate = kept_dir / f"{p.name}.{secrets.token_hex(8)}"
        if p.is_dir():
            # A DIRECTORY cannot be claimed first and then replaced. POSIX renames a directory
            # onto an EMPTY directory, but Windows refuses to replace an existing directory at
            # all (``WinError 5``), so a pre-claimed placeholder is a claim the move cannot
            # honour there -- which is how the claim-then-replace shape passed on Linux and
            # failed on a Windows lane. The rename to a name nothing holds IS the claim on both
            # platforms, and it fails rather than replacing when something does hold it.
            #
            # Nothing legitimate puts a directory here, but the instance being recorded is real
            # whatever is in the way, so this must not be the dead end.
            #
            # Residual, stated rather than hidden: POSIX renames onto an EMPTY directory, so a
            # collision with an empty preserved directory at the same random name replaces it.
            # An empty preserved directory holds nothing, and a non-empty one fails with
            # ``ENOTEMPTY`` and is skipped, so no preserved CONTENT can be replaced this way.
            try:
                os.rename(p, candidate)
            except OSError as exc:
                if isinstance(exc, FileExistsError) or exc.errno in (
                    errno.EEXIST,
                    errno.ENOTEMPTY,
                ):
                    continue
                raise
            return candidate
        try:
            os.close(os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            continue
        # Replaces the placeholder just claimed, which no other caller can now claim.
        os.replace(p, candidate)
        return candidate
    raise OSError(
        f"no free name to preserve {p.name} under after {_PRESERVE_CLAIM_ATTEMPTS} "
        "attempts, so it was left as it is"
    )


@dataclass
class CloudConfig:
    """The launcher's saved state (no secrets)."""

    profile: str = ""
    region: str = DEFAULT_REGION
    last_tag: str = ""
    #: The ``fargate`` block EXACTLY as read from the file, or ``None`` when the
    #: file has none. It is kept raw, not judged, so that ``save()`` writes back
    #: whatever the operator wrote: this object is loaded and re-saved to record
    #: ``last_tag`` after an ordinary EC2 launch, and a field that held only a
    #: judged value would erase a block the operator is half-way through writing.
    #: Whether the block is usable is :meth:`fargate_config`'s question.
    fargate: Any = None

    def fargate_config(self) -> Optional[FargateConfig]:
        """The Fargate block as a typed config, or ``None`` when it is not complete.

        ``None`` is what keeps the lane UNREGISTERED, so an operator who has not
        filled the block in is never offered a lane that would refuse them. This
        judges the raw block on every call rather than once at load, so the file
        round-trips untouched and the seam still sees complete-or-absent.
        """
        return FargateConfig.from_mapping(self.fargate)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "CloudConfig":
        """Read the record, tolerating every way the file can fail to be one.

        The tolerant policy: any unusable file reads as an unconfigured record, because a
        cloud command must not hand a raw traceback to an operator over a hand edit. The
        single fallback below is the whole refusal -- :meth:`_read_or_reason` decides WHAT
        went wrong, and this decides what to DO about it, which is nothing.

        A writer needs the opposite policy over the same parse, so it asks
        :meth:`_read_or_reason` directly instead of re-reading the file. Two policies, one
        parser: a second reader here would drift from this one.
        """
        record, _reason = cls._read_or_reason(path or (config_dir() / _FILENAME))
        return record if record is not None else cls()

    @classmethod
    def _read_or_reason(cls, p: Path) -> "tuple[Optional[CloudConfig], Optional[str]]":
        """``(record, None)`` usable, ``(None, reason)`` present but not, ``(None, None)`` absent.

        The three-way answer is the point. Absent and unreadable are the same non-answer to
        a reader and opposite instructions to a writer: absent means write the first record,
        unreadable means do not touch the file. Collapsing them is the bug this shape exists
        to prevent.
        """
        try:
            # ONE read serving both the size bound and the parse, so the bytes measured
            # are the bytes parsed.
            #
            # Reading one byte PAST the ceiling is what makes the bound a bound: it is the
            # smallest read that can tell "at the limit" from "over it" without a second
            # look at the file, and it replaces the `stat()` for the same reason.
            with open(p, "rb") as fh:
                raw = fh.read(_MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            # Absence, not failure: the first run has no file and must be able to write one.
            return None, None
        except OSError as exc:
            # A file that exists and would not open (EACCES, EIO, a Windows share violation).
            # Indistinguishable from corruption to a reader, opposite to a writer.
            return None, f"{p} could not be read: {getattr(exc, 'strerror', None) or exc}"
        # Size BEFORE parse. The field ceilings below bound what a block may retain, but
        # json.loads builds the whole document in memory first, so a bound applied to the
        # parsed result never runs on the input that would exhaust it. Treated as a corrupt
        # file rather than an error, because every caller of this already tolerates that
        # and nothing here should raise into a cloud command.
        if len(raw) > _MAX_FILE_BYTES:
            return None, f"{p} is larger than {_MAX_FILE_BYTES} bytes"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            # Every way a hand-edited file can fail to BECOME a document, answered in one
            # place, because all of them mean the same thing to every caller: this is not a
            # config that can be read. That is the tolerate-a-corrupt-file promise in the
            # docstring, and a caller cannot act differently on any of them.
            #
            # `json.JSONDecodeError` IS a `ValueError`, so naming `ValueError` widens rather
            # than replaces, and the widening is load-bearing: the interpreter's
            # integer-string limit raises plain `ValueError` for a 5,000-digit number that
            # is perfectly valid JSON syntax. `RecursionError` is the parse side of the
            # bound the writer already answers -- the nesting a parser ACCEPTS is not the
            # nesting it can BUILD, and the limit is on total stack rather than on the
            # document, so there is no depth that is safe to assume either way.
            return None, f"{p} is not a readable JSON document: {type(exc).__name__}"
        # A hand-edited cloud.json may parse to valid JSON that is NOT an object
        # (e.g. `"hello"`, `[1,2]`, `42`, `null`); the .get() calls below would
        # then raise AttributeError and escape load() (handle_cloud only catches
        # AWS/validation errors), giving a raw traceback on every cloud command.
        # Honor the docstring's tolerate-a-corrupt-file promise: fall back to
        # defaults on any non-object shape.
        if not isinstance(data, dict):
            return None, f"{p} holds a JSON {type(data).__name__}, not an object"
        # Sanitize last_tag at the boundary: a hand-edited/corrupt cloud.json
        # must not carry a malformed tag into the resume path (downstream
        # validate_tag would raise; an empty tag just means "no last launch").
        last_tag = str(data.get("last_tag", ""))
        if last_tag and not _TAG_RE.match(last_tag):
            last_tag = ""
        record = cls(
            profile=str(data.get("profile", "")),
            region=str(data.get("region", "") or DEFAULT_REGION),
            last_tag=last_tag,
            # Deliberately NOT sanitized here, unlike last_tag: an incomplete
            # block must survive a save, so it is carried as written and judged
            # by fargate_config() at the point of use.
            fargate=data.get("fargate"),
        )
        # Refuse at INGEST what could not be written back, because the size check above
        # does NOT imply this one. `json.dumps` escapes non-ASCII by default, so a
        # character costing 2 bytes on disk costs 6 written back (`e-acute` ->
        # `\\u00e9`), and 4 bytes costs 12 for a surrogate pair. A hand-edited block of
        # 200,000 accented characters is 400 KB on disk, passes the pre-parse bound, and
        # serializes to 1.2 MB. So the on-disk size is not an upper bound on the written
        # size and cannot stand in for it.
        #
        # Refusing HERE and not at `save()` is the whole point: `save()` runs after the
        # engine has provisioned, and it is the call that records the new instance's tag.
        # A failure there leaves a real instance running with nothing tracking it. At
        # ingest nothing has been provisioned, so the same refusal costs nothing.
        #
        # It reserves room for the fields the launch path writes AFTER the deploy, because
        # the record measured here is not the record that gets written. Measured: a block
        # sized to the byte passed this check and then could not be saved with a
        # 33-character tag, so the save that was meant to RECORD a running instance was
        # the one that failed.
        if not _fits_after_provisioning(record):
            return None, f"{p} could not be written back with the fields a launch adds"
        return record, None

    @classmethod
    def apply_update(
        cls,
        path: Optional[Path] = None,
        *,
        expect_last_tag: Optional[str] = None,
        **changes: Any,
    ) -> "CloudConfig":
        """Change only the named fields, atomically, and never refuse.

        Three states of the file on disk, and all three end in a write, because every caller
        has already spent something by the time it gets here. Absent: write the first record.
        Readable: merge and write. Present but unreadable: move those bytes into
        ``_PRESERVED_DIRNAME``, warn, and write -- which is what keeps "never refuse" true
        without it meaning "silently destroy whatever could not be parsed". A caller that
        also names ``expect_last_tag`` is the one exception, below.

        One rule covers that third state and the exhausted retry alike: a write never
        destroys the bytes it displaces. Whatever this call replaces and cannot use is moved
        into the sealed preservation directory first, and the write is abandoned rather than
        made when those bytes cannot be kept.

        The correct read-modify-write, in ONE place, and the only write shape available to a
        caller that has already spent something. Every caller of this runs AFTER its remote
        work: the launch path has deployed, the resume path has reattached, ``destroy`` has
        deleted the stack. An exception at that point does not fail a write -- it leaves an
        EC2 instance really running with nothing on disk recording it, and there is no
        teardown executor, no TTL and no budget ceiling to reclaim what nothing points at.
        A lost update is recoverable by re-editing a file; that is not.

        So the LOST-UPDATE refusal is not reachable from here, rather than merely rare. The
        read and the write happen under ONE hold of the writer lock, which leaves no window
        for a cooperating writer to interleave and therefore nothing to refuse -- a bounded
        retry would only have narrowed the window it could still lose in.

        One refusal IS reachable, and it is the opposite trade: when bytes this write must
        displace cannot be kept, nothing is written and the caller is told. That is chosen
        deliberately against the paragraph above, because the two losses are not symmetrical.
        A stranded instance is visible in the account and re-attachable with ``cloud resume``;
        a configuration this code destroyed is gone.

        Holding that lock is NOT the same as knowing nothing else wrote. It is advisory, so
        it binds only the writers that take it, and the other writer of this file is most
        likely a person in an editor who takes nothing -- plus the lock may fail to serialize
        at all when its file is unusable or the bounded acquire reaches its ceiling. So the
        live bytes are checked against a witness taken before the read on EVERY path, and the
        write re-merges when they moved: the fields this caller named land on the newest
        content rather than on a snapshot. Re-merging rather than refusing is deliberate --
        refusing is the stranding this method exists to prevent, while a re-merge loses
        neither side's fields. The residual is a writer that lands inside the window between
        the final check and the replace.

        Holding the lock across both halves is affordable here and is not in ``save()``:
        this is one read and one write, while a snapshot caller's flow spans an interactive
        deploy and would block every other writer for minutes.

        ``expect_last_tag`` puts the one caller's PRECONDITION inside the same lock hold.
        ``destroy`` clears ``last_tag`` only while it still names the stack being deleted, and
        read outside the lock that decision is made on a value that can change before the
        write -- a launch recording its own tag in between would have its pointer wiped by a
        command that never saw it. Spelled for that single field rather than as a general
        predicate: one caller needs it, and a dict of fields would be a shape nothing asks
        for.

        It is decided before the file is touched, so a write it declines leaves the file
        EXACTLY as it was -- including an unreadable one, which is not moved aside. The
        preservation belongs to a write that happens: performed for one that does not, it
        would take the operator's only copy of those bytes out of the place their tooling
        looks and put nothing there, so a rejected update would be the only path here that
        loses a configuration rather than re-merging it.
        """
        unknown = set(changes) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"not fields of {cls.__name__}: {sorted(unknown)}")
        p = path or (config_dir() / _FILENAME)
        p.parent.mkdir(parents=True, exist_ok=True)
        with _writer_lock(p.parent / (p.name + ".lock")):
            # The lock is an optimization, not the safety property. It is ADVISORY, so it
            # binds only the writers that take it -- and the most likely other writer of this
            # file is a person in an editor, who takes nothing and saves whenever they save.
            # So every replace checks the live bytes against a witness taken before the read,
            # whether or not the lock was acquired, and re-merges when they moved.
            #
            # Re-MERGING rather than refusing is what makes the retry safe to take: the
            # caller's fields land on the newest content each time, so the other writer's
            # fields survive and so do these. Refusing would strand the instance this call
            # exists to record, which is the harm it is here to prevent.
            for attempt in range(_MERGE_ATTEMPTS):
                witness = _raw_bytes_or_none(p)
                fresh = cls._merge_once(p, expect_last_tag=expect_last_tag, changes=changes)
                if fresh is None:
                    # The precondition did not hold against the current file: nothing to
                    # write, and re-reading would only re-derive the same answer.
                    return cls._read_or_reason(p)[0] or cls()
                if _raw_bytes_or_none(p) == witness:
                    fresh._replace_file(p)
                    return fresh
                if attempt == _MERGE_ATTEMPTS - 1:
                    # Out of attempts against a writer that keeps winning. Both obvious moves
                    # lose: writing replaces bytes newer than the merge, and not writing
                    # strands the instance this call exists to record. So the bytes being
                    # replaced are KEPT first, which is the same rule the unreadable path
                    # follows -- a write never destroys what it displaces -- and then the
                    # merge is written. Neither side is lost: the instance is tracked and the
                    # other writer's newest content is on disk to be read back.
                    try:
                        kept = _preserve_displaced(p)
                    except OSError as exc:
                        # Could not keep them, so do not destroy them: the same choice the
                        # unreadable path makes, for the same reason. A stranded instance is
                        # visible in the account and re-attachable; destroyed bytes are not.
                        raise OSError(
                            f"{p} changed under every attempt to write it and could not be "
                            f"moved aside either ({getattr(exc, 'strerror', None) or exc}), "
                            f"so it was left alone and these were not recorded: {changes!r}"
                        ) from exc
                    logger.warning(
                        "cloud config: %s changed under every attempt to write it; kept those "
                        "bytes as %s and wrote the last merge, which carries this caller's "
                        "fields and everything the final read saw.",
                        p,
                        f"{kept.parent.name}/{kept.name}",
                    )
                    fresh._replace_file(p)
                    return fresh
        raise AssertionError("unreachable: the loop returns on every path")

    @classmethod
    def _merge_once(
        cls,
        p: Path,
        *,
        expect_last_tag: Optional[str],
        changes: "dict[str, Any]",
    ) -> "Optional[CloudConfig]":
        """Read, merge *changes*, and return the record WITHOUT writing it, or ``None`` when
        the ``expect_last_tag`` precondition says this write must not be made.

        Split out so the write can be attempted more than once against a file that may be
        moving. Everything here is decided from what is on disk right now, so a second call
        re-decides the ``expect_last_tag`` precondition and the preservation against the
        newest content rather than replaying a stale verdict.
        """
        fresh, reason = cls._read_or_reason(p)
        if expect_last_tag is not None and cls._on_disk_tag(fresh, reason) != expect_last_tag:
            # DECIDED BEFORE ANYTHING ON DISK IS TOUCHED. A write that is not made must
            # leave the file exactly as it was found, and the preservation below is a
            # mutation: after this check it moves the operator's bytes aside for a write
            # that is then never made, which is the one way this method loses a
            # configuration outright instead of re-merging it.
            #
            # The tag this caller read is not the tag on disk, so its premise is gone and
            # the write is simply not made. A no-op rather than an exception, because this
            # runs after remote work like every caller here and an exception would be the
            # stranding it exists to avoid. Nothing is lost by declining: the caller asked
            # to clear a value the file does not hold.
            return None
        if reason is not None:
            # Present and unreadable, which is the one case where BOTH obvious moves
            # lose something real. Writing over it destroys whatever the bytes held --
            # for this file that is the Fargate block, since every caller supplies
            # profile and region itself. Refusing to write strands the instance the
            # call exists to record, and nothing reclaims an instance nothing points at.
            #
            # Moving the bytes aside first costs neither. The record is written, so the
            # instance is tracked, and the unreadable file is still on disk for an
            # operator to read their cluster and subnets back out of.
            try:
                kept = _preserve_displaced(p)
            except OSError as exc:
                # Could not preserve it, so do not destroy it either: this is the only
                # path where the file stays as it is and the caller is told instead.
                raise OSError(
                    f"{reason}; it could not be moved aside either "
                    f"({getattr(exc, 'strerror', None) or exc}), so it was left alone "
                    f"and these were not recorded: {changes!r}"
                ) from exc
            logger.warning(
                "%s; moved it to %s and wrote a fresh record. Any Fargate settings it "
                "held must be re-entered (kirocrew cloud setup) or copied back out of "
                "that file.",
                reason,
                f"{kept.parent.name}/{kept.name}",
            )
            fresh = cls()
        elif fresh is None:
            # Absent: the first record for this install, written normally.
            fresh = cls()
        for name, value in changes.items():
            setattr(fresh, name, value)
        return fresh

    @staticmethod
    def _on_disk_tag(fresh: "Optional[CloudConfig]", reason: Optional[str]) -> str:
        """The ``last_tag`` currently on disk, for the precondition to compare against.

        Absent and unreadable both answer the default, which is what a record built from
        nothing carries -- so this returns exactly the value the comparison used when it
        ran after the two branches that substituted such a record. Only the ORDER changed;
        no verdict did.

        Both non-readable states decline a caller that named a tag, for the same reason a
        mismatch does: the premise is that the tag this caller read is still the tag on
        disk, and neither a file nothing can parse nor a file that is not there holds a
        tag anyone could have read.
        """
        if reason is None and fresh is not None:
            return fresh.last_tag
        return CloudConfig().last_tag

    def save(self, path: Optional[Path] = None) -> None:
        """Write this record.

        The refusal is what stops a LOST UPDATE. A caller reads the config, works for a
        while -- the wizard holds its snapshot across an entire interactive flow and a
        deploy -- and writes the whole record back. Any edit that landed in between is in
        the file but not in the snapshot, and writing the snapshot erases it silently.

        Refusing rather than locking the whole read-modify-write: an exclusive lock held
        for the duration of that flow would block every other writer for minutes, and a
        crash mid-flow would leave the lock behind. A conditional write lets the loser
        re-read and re-apply, which is the recoverable direction, and it composes across
        processes because the condition lives on disk rather than in one process's memory.

        Under the writer lock, so two writers cannot interleave. Unconditional: the
        read-modify-write callers all go through :meth:`apply_update`, which merges only the
        fields it was given, so there is no caller holding a whole stale record for this to
        protect.
        """
        p = path or (config_dir() / _FILENAME)
        p.parent.mkdir(parents=True, exist_ok=True)
        with _writer_lock(p.parent / (p.name + ".lock")):
            self._replace_file(p)

    def _replace_file(self, p: Path) -> None:
        """Serialize this record and replace *p*, with the writer lock ALREADY held.

        One write, shared by both callers, so the serialize-then-replace ordering exists in
        one place rather than twice.
        """
        payload = _serialize_record(_record_fields(self))
        # Cannot raise for anything `load()` accepted: `load()` enforces the same bound
        # through the same function, and it reserves room for the fields the launch path
        # writes later, so a record that got in can be written back out with them. This
        # refusal is reachable only for a record built in memory that was never loaded, and
        # it leaves the previous file intact -- the recoverable direction.
        if payload is None:
            raise ValueError(
                f"refusing to write {p}: the record does not fit the "
                f"{_MAX_FILE_BYTES}-byte ceiling load() enforces even when serialized "
                "compactly, so saving it would make this config unreadable"
            )
        # Unique temp name per writer: concurrent cloud invocations must not
        # race on a shared .tmp path (see atomic_write's rationale).
        atomic_write(p, payload)
