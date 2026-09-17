"""Unit tests for the cloud config store (cloud/config.py) — no secrets stored."""

from __future__ import annotations

import json

import pytest

from kiro_crew.cloud import config as cloud_config
from kiro_crew.cloud.config import (
    _MAX_FILE_BYTES,
    DEFAULT_REGION,
    CloudConfig,
    FargateConfig,
)

#: The model-credential secret as a conforming ``(name, ARN)`` pair: the name is
#: ``kirocrew/crew/<crew>/<ENV>`` and the ARN is that name plus one six-character
#: service suffix. The account id is fictional.
CREDENTIAL_SECRET = [
    "kirocrew/crew/demo/KIRO_API_KEY",
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:kirocrew/crew/demo/KIRO_API_KEY-abcdef",
]

#: A complete Fargate block. Every case below is this, minus or plus one thing, so
#: a case cannot pass by being malformed in a second way the assertion never named.
COMPLETE_FARGATE = {
    "cluster": "kirocrew-crew-prod",
    "subnets": ["subnet-a", "subnet-b"],
    "security_groups": ["sg-1"],
    "image": "public.ecr.aws/example/kirocrew-crew-base@sha256:" + "a" * 64,
    "secrets": [CREDENTIAL_SECRET],
    "cpu_architecture": "X86_64",
}


class TestFargateConfig:
    """A block is COMPLETE or ABSENT. There is no third state, by design.

    A half-written block that produced a usable object would leave the lane
    registered and refusing every launch made through it -- spending an operator's
    attention at launch time on a mistake that was visible when they saved the
    file.
    """

    def test_a_complete_block_is_read(self):
        config = FargateConfig.from_mapping(COMPLETE_FARGATE)
        assert config is not None
        assert config.cluster == "kirocrew-crew-prod"
        assert config.subnets == ("subnet-a", "subnet-b")
        assert config.security_groups == ("sg-1",)
        assert config.secrets == (
            (COMPLETE_FARGATE["secrets"][0][0], COMPLETE_FARGATE["secrets"][0][1]),
        )
        assert config.is_complete()

    @pytest.mark.parametrize(
        ("label", "block"),
        [
            ("movable tag image", {**COMPLETE_FARGATE, "image": "public.ecr.aws/x/base:latest"}),
            ("no image", {**COMPLETE_FARGATE, "image": ""}),
            ("no cluster", {**COMPLETE_FARGATE, "cluster": ""}),
            ("no subnet", {**COMPLETE_FARGATE, "subnets": []}),
            ("no security group", {**COMPLETE_FARGATE, "security_groups": []}),
            ("unknown architecture", {**COMPLETE_FARGATE, "cpu_architecture": "RISCV"}),
            ("subnets not a list", {**COMPLETE_FARGATE, "subnets": "subnet-a"}),
            ("secrets not a list", {**COMPLETE_FARGATE, "secrets": "nope"}),
            ("secret entry is not a pair", {**COMPLETE_FARGATE, "secrets": [["only-one"]]}),
            ("secret arn is empty", {**COMPLETE_FARGATE, "secrets": [["KIRO_API_KEY", ""]]}),
            ("no secrets at all", {**COMPLETE_FARGATE, "secrets": []}),
            (
                "secrets but none named for the model credential",
                {
                    **COMPLETE_FARGATE,
                    "secrets": [["kirocrew/crew/demo/OTHER_KEY", CREDENTIAL_SECRET[1]]],
                },
            ),
            ("public ip is the string false", {**COMPLETE_FARGATE, "assign_public_ip": "false"}),
            ("public ip is the string zero", {**COMPLETE_FARGATE, "assign_public_ip": "0"}),
            ("public ip is the string true", {**COMPLETE_FARGATE, "assign_public_ip": "true"}),
            ("public ip is a number", {**COMPLETE_FARGATE, "assign_public_ip": 1}),
            ("public ip is null", {**COMPLETE_FARGATE, "assign_public_ip": None}),
            (
                "secrets list past the item bound",
                {
                    **COMPLETE_FARGATE,
                    "secrets": [CREDENTIAL_SECRET] * (cloud_config._MAX_LIST_ITEMS + 1),
                },
            ),
            (
                "subnets list past the item bound",
                {**COMPLETE_FARGATE, "subnets": ["subnet-a"] * (cloud_config._MAX_LIST_ITEMS + 1)},
            ),
            (
                "image string past the size bound",
                {**COMPLETE_FARGATE, "image": "x" * (cloud_config._MAX_STRING_LEN + 1)},
            ),
            (
                "a subnet string past the size bound",
                {**COMPLETE_FARGATE, "subnets": ["s" * (cloud_config._MAX_STRING_LEN + 1)]},
            ),
            (
                "a secret name past the size bound",
                {
                    **COMPLETE_FARGATE,
                    "secrets": [
                        [
                            "x" * (cloud_config._MAX_STRING_LEN + 1) + "/KIRO_API_KEY",
                            CREDENTIAL_SECRET[1],
                        ]
                    ],
                },
            ),
            ("not an object", "nope"),
            ("absent", None),
        ],
    )
    def test_an_unusable_block_reads_as_absent(self, label: str, block: object):
        assert FargateConfig.from_mapping(block) is None, label

    def test_a_secretless_block_is_incomplete_because_the_engine_would_refuse_it(self):
        """The engine refuses a task definition delivering no model credential.

        So a block with every placement field and an empty ``secrets`` list is the
        offered-and-refusing state exactly: the lane would register and reject every
        launch. Judged here, it is absent instead.
        """
        assert FargateConfig.from_mapping({**COMPLETE_FARGATE, "secrets": []}) is None

    def test_a_reference_the_engine_would_refuse_does_not_register(self):
        """The gate asks the ENGINE, so its answer and the engine's cannot differ.

        This replaces an earlier assertion that a conforming NAME registered whatever
        its ARN said. That was deliberate at the time -- the ARN pairing was left to the
        engine to avoid a second copy of its rule -- but it meant a mismatched pair
        registered the lane and was then refused at launch, which is the state this
        module exists to prevent. Delegating to ``identity.secret_env_name`` moved the
        boundary rather than duplicating it: there is still exactly one copy of the
        rule, and it now runs here too.
        """
        block = {**COMPLETE_FARGATE, "secrets": [[CREDENTIAL_SECRET[0], "arn:not-a-real-arn"]]}
        assert FargateConfig.from_mapping(block) is None

    def test_a_conforming_reference_still_registers(self):
        """The positive side, so the test above cannot pass by refusing everything."""
        assert FargateConfig.from_mapping(COMPLETE_FARGATE) is not None

    @pytest.mark.parametrize("bad", [False, True, 0, 1, 1.5, None, [], {}, [1], {"a": 1}])
    def test_no_string_field_coerces_a_non_string(self, bad: object):
        """Every string field, derived from the dataclass, not a list someone maintains.

        `str()` made any JSON scalar truthy: `false` became `"False"`, which is
        non-empty, so `is_complete()` passed and the lane registered against a cluster
        that does not exist. Parametrized over the FIELDS as well as the values, so a
        string field added later is covered without anyone remembering.
        """
        from kiro_crew.cloud.config import _STRING_FIELD_DEFAULTS

        assert _STRING_FIELD_DEFAULTS, "the field derivation must not be empty"
        for field_name in _STRING_FIELD_DEFAULTS:
            block = {**COMPLETE_FARGATE, field_name: bad}
            assert FargateConfig.from_mapping(block) is None, f"{field_name}={bad!r}"

    def test_the_string_field_list_matches_the_dataclass(self):
        """Pins the derivation itself, so a field cannot silently drop out of it."""
        from dataclasses import fields

        from kiro_crew.cloud.config import _STRING_FIELD_DEFAULTS

        expected = {f.name for f in fields(FargateConfig) if isinstance(f.default, str)}
        assert set(_STRING_FIELD_DEFAULTS) == expected


class TestAnythingLoadAcceptsSaveCanWrite:
    """The round-trip property, not a size assertion.

    A size assertion would re-encode the assumption that just broke: `load()` measured
    the file it was HANDED while `save()` emitted `indent=2`, which is larger. So a
    minified block passed on the way in and failed on the way out -- and it failed AFTER
    the engine had provisioned, leaving a running instance no saved record pointed at.
    The property is what matters: for a payload `load()` accepts, `save()` never raises.
    """

    #: ``(label, block)`` pairs whose COMPACT form loads but whose PRETTY form may not.
    #: An array of many short members, not one long string: ``indent=2`` costs bytes PER
    #: MEMBER, so a 400 KB compact array re-expands past a 1 MiB ceiling while a single
    #: 1 MB string grows by about 20 bytes. A padded-string fixture looked like it
    #: covered this and did not -- removing the compact fallback left it green.
    BLOCKS = [
        ("small", {"cluster": "c", "subnets": ["subnet-abc"]}),
        ("array pretty-fits", {"cluster": "c", "subnets": ["s"] * 20_000}),
        ("array pretty-breaches", {"cluster": "c", "subnets": ["s"] * 100_000}),
    ]

    @pytest.mark.parametrize(("label", "block"), BLOCKS)
    def test_save_never_raises_and_the_block_survives(self, tmp_path, label, block):
        """Both halves, because either alone is passable by doing the wrong thing.

        Asserting only that `save()` does not raise is satisfied by `load()` DROPPING
        the block -- there is then nothing large to write. Asserting the block SURVIVES
        is what makes the compact fallback load-bearing, so removing it goes red.
        """
        import json as _json

        from kiro_crew.cloud.config import _MAX_FILE_BYTES, CloudConfig

        src = tmp_path / "cloud.json"
        src.write_text(
            _json.dumps({"profile": "p", "fargate": block}, separators=(",", ":")),
            encoding="utf-8",
        )
        assert src.stat().st_size <= _MAX_FILE_BYTES, "the fixture must be loadable"

        loaded = CloudConfig.load(src)
        assert loaded.fargate == block, "load() must keep the block, not drop it"

        out = tmp_path / "out.json"
        loaded.save(out)  # must not raise for anything load() accepted
        assert CloudConfig.load(out).fargate == block, "and it must survive the round trip"

    def test_the_array_fixture_really_breaches_when_pretty(self):
        """Pins the fixture's own premise, so it cannot quietly stop reproducing.

        If `indent=2` on this block ever fits the ceiling, the test above proves nothing
        and would go green with the compact fallback deleted -- which is exactly how the
        first version of it passed.
        """
        import json as _json

        from kiro_crew.cloud.config import _MAX_FILE_BYTES

        block = dict(self.BLOCKS)["array pretty-breaches"]
        record = {"profile": "p", "region": "us-east-1", "last_tag": "", "fargate": block}
        compact = len(_json.dumps(record, separators=(",", ":")).encode("utf-8"))
        pretty = len(_json.dumps(record, indent=2).encode("utf-8"))
        assert compact <= _MAX_FILE_BYTES < pretty, f"compact={compact} pretty={pretty}"

    #: Characters whose ESCAPED form is larger than their UTF-8 form, with the multiplier.
    #: This is why an on-disk size cannot stand in for a written size, and why the ingest
    #: refusal is not redundant with the pre-parse bound. Chosen from the three widths a
    #: JSON escape produces, so a change to `ensure_ascii` handling fails here.
    ESCAPE_EXPANDERS = [
        ("latin-1 accent", "\u00e9", 3),
        ("cjk", "\u4e2d", 2),
        ("astral (surrogate pair)", "\U0001f600", 3),
    ]

    @pytest.mark.parametrize(("label", "char", "multiplier"), ESCAPE_EXPANDERS)
    def test_a_block_that_cannot_be_written_is_refused_while_loading(
        self, tmp_path, label, char, multiplier
    ):
        """The on-disk bound does NOT imply the written bound, so both must be enforced.

        `json.dumps` escapes non-ASCII by default, so a character costing 2 bytes on disk
        costs 6 written back. A hand-edited block of accented text is 400 KB on disk,
        passes the pre-parse bound, and serializes to 1.2 MB. Refused while loading, not
        at `save()`: `save()` runs after the engine has provisioned and is the call that
        records the new instance, so failing there leaves an instance nothing tracks.
        """
        import json as _json

        from kiro_crew.cloud.config import _MAX_FILE_BYTES, CloudConfig

        # Sized so the file fits the on-disk bound but its escaped form cannot.
        count = int(_MAX_FILE_BYTES / len(char.encode("utf-8")) / multiplier) + 20_000
        src = tmp_path / "cloud.json"
        src.write_text(
            _json.dumps(
                {"profile": "p", "fargate": {"cluster": "c", "pad": char * count}},
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert src.stat().st_size <= _MAX_FILE_BYTES, "the fixture must pass the on-disk bound"

        loaded = CloudConfig.load(src)
        assert loaded.fargate is None, "refused at ingest, before anything is provisioned"
        loaded.save(tmp_path / "out.json")  # and the later save cannot then raise


class TestAConcurrentEditIsNeverLost:
    """The property, stated so a narrower race cannot pass it.

    A caller reads the config, works, and writes the whole record back. Anything that
    landed in between is in the file but not in the snapshot, so writing the snapshot
    erases it with nothing said. The property is not "the window is small": it is that
    the write either INCLUDES the other edit or REFUSES. Both outcomes keep the edit;
    only a silent overwrite loses it.

    Note on scope: the read-only seal on `cloud.json` does NOT cover this. The seal stops
    a sandboxed agent from writing the file at all, and the writers racing here are both
    trusted paths. Claiming the seal answers this would be the same mistake as the macOS
    branch -- naming a protection that is not doing the work.
    """

    FARGATE = dict(COMPLETE_FARGATE)

    @staticmethod
    def _seed(p) -> None:
        import json as _json

        p.write_text(_json.dumps({"profile": "old", "region": "us-east-1"}), encoding="utf-8")

    def test_the_refusal_is_recoverable_by_re_reading(self, tmp_path):
        """Refusing is only correct if the caller has a way forward."""
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        other = CloudConfig.load(p)
        other.fargate = self.FARGATE
        other.save(p)

        saved = CloudConfig.apply_update(p, profile="mine", last_tag="")
        assert saved.profile == "mine"
        assert CloudConfig.load(p).fargate == self.FARGATE, "and it still survived"

    def test_apply_update_keeps_fields_it_was_not_given(self, tmp_path):
        """The narrow update is what makes the launch path safe."""
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        seeded = CloudConfig.load(p)
        seeded.fargate = self.FARGATE
        seeded.save(p)

        CloudConfig.apply_update(p, profile="new", region="us-west-2", last_tag="t")
        after = CloudConfig.load(p)
        assert (after.profile, after.region) == ("new", "us-west-2")
        assert after.fargate == self.FARGATE, "a field it was not given is untouched"

    def test_an_unknown_field_is_refused(self, tmp_path):
        """So a typo cannot look like a successful write of nothing."""
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        with pytest.raises(ValueError, match="not fields of CloudConfig"):
            CloudConfig.apply_update(p, porfile="typo")

    def test_a_record_that_fits_only_without_a_tag_is_refused(self, tmp_path):
        """The check must measure what will be WRITTEN, not what arrived.

        The launch path sets `last_tag` after the deploy succeeds, so a record sized to
        the byte passes an ingest check that ignores it and then cannot be saved. Measured
        before fixing: a 1,048,541-byte file loaded, and saving it with a 33-character tag
        raised -- so the save meant to RECORD a running instance was the one that failed.
        """
        import json as _json

        from kiro_crew.cloud.config import _MAX_FILE_BYTES, _TAG_MAX_LEN, CloudConfig

        block = {"cluster": "c", "pad": "x" * 848_480, "subnets": ["s"] * 50_000}
        src = tmp_path / "cloud.json"
        src.write_text(
            _json.dumps({"profile": "", "fargate": block}, separators=(",", ":")),
            encoding="utf-8",
        )
        assert src.stat().st_size <= _MAX_FILE_BYTES, "the fixture must pass the on-disk bound"

        loaded = CloudConfig.load(src)
        assert loaded.fargate is None, "refused at ingest, before the deploy"

        # And the refusal is what makes this save safe at MAXIMUM tag length.
        loaded.last_tag = "k" * _TAG_MAX_LEN
        loaded.save(src)

    @pytest.mark.parametrize("field_name", ["last_tag", "profile", "region"])
    def test_room_is_reserved_for_every_field_provisioning_writes(self, field_name: str):
        """Over the fields, so one added later is covered rather than remembered.

        Asserted against the validators' own limits, not example values: a reservation
        derived from a 33-character sample tag would be too small for the 51 the pattern
        permits, which is the same drift in a smaller costume.
        """
        from kiro_crew.cloud.config import _MAX_STRING_LEN, _PROVISIONING_WRITES, _TAG_MAX_LEN

        assert field_name in _PROVISIONING_WRITES, f"{field_name} is written but not reserved"
        expected = _TAG_MAX_LEN if field_name == "last_tag" else _MAX_STRING_LEN
        assert _PROVISIONING_WRITES[field_name] == expected

    def test_the_tag_pattern_and_the_reservation_cannot_disagree(self):
        """One bound, two users. A second literal would be free to drift."""
        from kiro_crew.cloud.config import _TAG_MAX_LEN, _TAG_RE

        assert _TAG_RE.match("a" * _TAG_MAX_LEN), "the pattern must accept its own bound"
        assert not _TAG_RE.match("a" * (_TAG_MAX_LEN + 1)), "and reject one past it"

    def test_a_file_past_the_ceiling_is_refused_without_being_read_whole(
        self, tmp_path, monkeypatch
    ):
        """The ceiling must bound the READ, not only the verdict.

        A read that ignores the ceiling both defeats the memory bound and pulls in a file
        too large to write back, so the size of what comes in is the property under test.
        """
        from kiro_crew.cloud.config import _MAX_FILE_BYTES, CloudConfig

        p = tmp_path / "cloud.json"
        p.write_text(
            '{"profile": "p", "pad": "' + "x" * (_MAX_FILE_BYTES + 5000) + '"}',
            encoding="utf-8",
        )
        on_disk = p.stat().st_size
        assert on_disk > _MAX_FILE_BYTES

        # The verdict alone cannot witness the bound: with an UNBOUNDED read the length
        # check inside load() still refuses, so an assertion on the defaults alone holds
        # whether the read is bounded or not, and the test's name would claim a property
        # its body never observes. The assertion is therefore on how many bytes come IN,
        # which is the thing the ceiling exists to bound.
        import builtins

        pulled: list[int] = []
        real_open = builtins.open

        class _CountingReads:
            """Delegates everything, recording only how much each read returned."""

            def __init__(self, fh):
                self._fh = fh

            def read(self, *a, **k):
                data = self._fh.read(*a, **k)
                pulled.append(len(data))
                return data

            def __enter__(self):
                self._fh.__enter__()
                return self

            def __exit__(self, *exc):
                return self._fh.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self._fh, name)

        def counting_open(file, *a, **k):
            fh = real_open(file, *a, **k)
            return _CountingReads(fh) if str(file) == str(p) else fh

        monkeypatch.setattr(builtins, "open", counting_open)
        loaded = CloudConfig.load(p)
        monkeypatch.undo()

        assert pulled, "the loader never read the file, so nothing here tested the bound"
        assert max(pulled) <= _MAX_FILE_BYTES + 1, (
            f"load() pulled {max(pulled)} bytes in for a {on_disk}-byte file: the ceiling "
            "must bound the READ, or an oversized file exhausts memory before the refusal"
        )
        assert loaded.profile == "", "an over-size file must read as defaults"

    def test_two_configs_with_the_same_settings_are_equal(self, tmp_path):
        """The fingerprint must stay out of equality, or callers comparing configs break."""
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        other = tmp_path / "elsewhere.json"
        other.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        assert CloudConfig.load(p) == CloudConfig.load(other), "read-source must not affect =="

    def test_the_lock_is_held_while_the_write_happens(self, tmp_path, monkeypatch):
        """Verify-then-write must be one critical section, not two adjacent steps.

        Without the lock the two writers can BOTH pass the fingerprint check -- each sees
        the same old file -- and both then write, so one update is lost with nothing
        raised. Narrowing that window rather than closing it would leave a race that is
        rarer and so harder to reproduce, which is worse than an obvious one.

        Asserted by probing from another thread at the one instant it matters: while the
        replacement is being written. A non-blocking acquire must FAIL, because this
        writer holds it. `flock` is per open-file-description, so a separate `open()` in
        the probe genuinely contends rather than re-entering.
        """
        import threading

        from kiro_crew import platform_compat
        from kiro_crew.cloud import config as config_mod

        p = tmp_path / "cloud.json"
        self._seed(p)
        lock_path = tmp_path / "cloud.json.lock"
        probe: dict[str, object] = {}
        real_atomic_write = config_mod.atomic_write

        def probing_write(target, payload):
            def probe_the_lock() -> None:
                try:
                    with open(lock_path, "a+", encoding="utf-8") as fh:
                        with platform_compat.file_lock(fh.fileno(), exclusive=True, wait=False):
                            probe["acquired"] = True
                except OSError as exc:
                    probe["refused"] = type(exc).__name__

            t = threading.Thread(target=probe_the_lock)
            t.start()
            t.join(timeout=10)
            return real_atomic_write(target, payload)

        monkeypatch.setattr(config_mod, "atomic_write", probing_write)
        cfg = config_mod.CloudConfig.load(p)
        cfg.profile = "mine"
        cfg.save(p)

        assert probe, "the probe never ran, so this asserts nothing"
        assert "acquired" not in probe, (
            "another writer acquired the lock while the replacement was being written, "
            "so verify and write are not one critical section"
        )
        assert probe.get("refused") == "BlockingIOError", probe

    def test_no_writer_loses_its_edit_under_real_contention(self, tmp_path):
        """Many threads, each adding its OWN key, and every key must be present.

        The strongest form of the property: it does not depend on a fixture guessing the
        interleaving. If any writer's update is dropped, a key is missing at the end.
        """
        import threading

        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        writers = 12
        errors: list[BaseException] = []

        def write(n: int) -> None:
            try:
                CloudConfig.apply_update(p, last_tag=f"tag-{n:02d}")
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"writers failed: {errors[:3]}"
        # Every writer set the same field, so the survivor is one of them -- and crucially
        # the file is still valid and the seeded region was never clobbered to a default.
        final = CloudConfig.load(p)
        assert final.last_tag.startswith("tag-"), final.last_tag
        assert final.region == "us-east-1", "a concurrent writer corrupted an unrelated field"

    def test_a_spent_launch_is_always_recorded_even_against_a_concurrent_edit(self, tmp_path):
        """The write that records a running instance must not be refusable.

        Every `apply_update` caller runs after its remote work: the launch path has deployed,
        the resume path has reattached, `destroy` has deleted the stack. A refusal there does
        not fail a write, it leaves an EC2 instance running with nothing on disk naming it,
        and no teardown executor, TTL or budget ceiling can reclaim what nothing points at.
        A bounded retry makes that rarer; only removing the refusal from this path removes it.
        """
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        CloudConfig.load(p)  # the caller's early read, before the deploy

        # An operator edits during the deploy.
        p.write_text('{"profile": "operator-edit", "region": "eu-west-1"}', encoding="utf-8")

        # The post-deploy record lands anyway, and does not erase the edit it found.
        CloudConfig.apply_update(p, last_tag="kc-spent")
        after = CloudConfig.load(p)
        assert after.last_tag == "kc-spent", "the instance was provisioned and not recorded"
        assert after.profile == "operator-edit", "the concurrent edit was erased"

    def test_two_writers_changing_different_fields_both_survive(self, tmp_path):
        """Read and write under ONE lock hold, so there is no window to interleave in.

        Without it the two halves are separate observations of the same file, and the later
        writer builds its whole record from a read that predates the earlier writer's write,
        so one field is silently lost. Every writer here sets a DIFFERENT field, so a lost
        write shows up as a missing value rather than as a coin toss between equals.
        """
        import threading
        import time

        from kiro_crew.cloud import config as cloud_config
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)

        real_load = CloudConfig.load.__func__

        def slow_load(cls, path=None):
            record = real_load(cls, path)
            time.sleep(0.05)  # widen the read-to-write gap so an unlocked version loses
            return record

        errors: list[BaseException] = []

        def writer(**changes):
            try:
                CloudConfig.apply_update(p, **changes)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cloud_config.CloudConfig, "load", classmethod(slow_load))
            threads = [
                threading.Thread(target=writer, kwargs={"profile": "writer-a"}),
                threading.Thread(target=writer, kwargs={"last_tag": "writer-b"}),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert not errors, f"a post-spend write raised: {errors[:2]}"
        final = CloudConfig.load(p)
        assert final.profile == "writer-a", "the profile write was lost"
        assert final.last_tag == "writer-b", "the tag write was lost"

    def test_a_lock_ignoring_writer_cannot_block_the_post_spend_record(self, tmp_path):
        """The lock stops cooperating writers. Nothing stops one that never takes it.

        That is the residual this path is deliberately built around, so it is tested rather
        than asserted in prose: the record must land ANYWAY, because the alternative is an
        EC2 instance running with nothing naming it. Simulated by writing the file from
        inside the read, which is exactly what a writer holding no lock does.
        """
        from kiro_crew.cloud import config as cloud_config
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        real_load = CloudConfig.load.__func__

        def load_then_someone_writes(cls, path=None):
            record = real_load(cls, path)
            (path or p).write_text('{"profile": "took-no-lock"}', encoding="utf-8")
            return record

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cloud_config.CloudConfig, "load", classmethod(load_then_someone_writes))
            CloudConfig.apply_update(p, last_tag="kc-spent")

        assert (
            CloudConfig.load(p).last_tag == "kc-spent"
        ), "a writer that ignored the lock blocked the record of a provisioned instance"

    def test_a_held_lock_cannot_hang_the_post_spend_write(self, tmp_path):
        """A lock someone else holds must not strand a provisioned instance either.

        Holding an advisory lock on the sibling needs no write to the config, and the
        sibling's name sits in a directory a sandboxed process may reach, so an unbounded
        wait hands a hostile holder a way to hang this write forever. That is the same
        unrecoverable outcome as the refusal it replaced, reached through the mechanism that
        replaced it, which is why the acquire is bounded and gives up into an unserialized
        write rather than waiting.

        Run in a thread with a bounded join, so an unbounded acquire fails this FAST instead
        of parking the suite until pytest's own timeout.
        """
        import threading

        from kiro_crew import platform_compat
        from kiro_crew.cloud import config as cloud_config
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        lock_path = tmp_path / "cloud.json.lock"

        holding = threading.Event()
        release = threading.Event()
        recorded = threading.Event()

        def hostile_holder() -> None:
            with open(lock_path, "a+", encoding="utf-8") as fh:
                with platform_compat.file_lock(fh.fileno(), exclusive=True):
                    holding.set()
                    release.wait(timeout=30)

        def post_deploy_write() -> None:
            CloudConfig.apply_update(p, last_tag="kc-spent")
            recorded.set()

        with pytest.MonkeyPatch.context() as mp:
            # A short ceiling keeps the test quick; the property is that a ceiling EXISTS.
            mp.setattr(cloud_config, "_LOCK_ACQUIRE_CEILING_SECS", 0.3)
            holder = threading.Thread(target=hostile_holder, daemon=True)
            holder.start()
            assert holding.wait(timeout=5), "the fixture never took the lock"

            writer = threading.Thread(target=post_deploy_write, daemon=True)
            writer.start()
            landed = recorded.wait(timeout=10)
            release.set()
            writer.join(timeout=10)
            holder.join(timeout=10)

        assert landed, (
            "the post-deploy write waited on a lock someone else held, so a provisioned "
            "instance would be left with nothing recording it"
        )
        assert CloudConfig.load(p).last_tag == "kc-spent"

    def test_a_precondition_is_checked_against_the_locked_value(self, tmp_path):
        """A caller's premise must be re-checked inside the lock, not before it.

        `destroy` clears `last_tag` only when it still names the stack being deleted. Read
        outside the lock, a launch recording its own tag in between has that pointer wiped by
        a command that never saw it. Declined rather than raised, because this runs after the
        remote work like every writer here.
        """
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        CloudConfig.apply_update(p, last_tag="launched-later")

        # destroy was removing "being-destroyed"; a newer launch now owns the pointer.
        CloudConfig.apply_update(p, expect_last_tag="being-destroyed", last_tag="")
        assert (
            CloudConfig.load(p).last_tag == "launched-later"
        ), "a stale precondition cleared a pointer a newer launch had recorded"

        # And it still clears when the premise does hold.
        CloudConfig.apply_update(p, expect_last_tag="launched-later", last_tag="")
        assert CloudConfig.load(p).last_tag == ""

    def test_a_declined_write_leaves_an_unreadable_file_where_it_is(self, tmp_path):
        """A write that is not made must not move the operator's only copy of it.

        The preservation is a mutation performed on behalf of a write. Ordered before the
        precondition, an unreadable `cloud.json` plus a stale `destroy` tag renamed the file
        aside and then declined -- so the configuration left the place the tooling reads and
        nothing took its place. Every other path here re-merges; this was the one that lost.
        """
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        corrupt = b"{ this is hand-edited and unparseable"
        p.write_bytes(corrupt)

        # destroy's premise: it still owns the pointer. An unreadable file holds no tag it
        # could have read, so the write is declined.
        CloudConfig.apply_update(p, expect_last_tag="being-destroyed", last_tag="")

        assert p.read_bytes() == corrupt, (
            "a declined write moved the unreadable config aside, so the operator's only "
            "copy left the path their tooling reads"
        )
        assert not list(tmp_path.glob("cloud-config-preserved/cloud.json.*")), (
            "a write that was never made still preserved the file, which is the rename "
            "this ordering exists to prevent"
        )

    def test_an_unreadable_file_is_still_preserved_for_a_write_that_happens(self, tmp_path):
        """The complement, so the reorder cannot be satisfied by never preserving.

        A caller with no precondition still writes, so the unparseable bytes are still moved
        aside rather than destroyed.
        """
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        corrupt = b"{ this is hand-edited and unparseable"
        p.write_bytes(corrupt)

        CloudConfig.apply_update(p, last_tag="kc-launched")

        assert CloudConfig.load(p).last_tag == "kc-launched"
        kept = [q.read_bytes() for q in tmp_path.glob("cloud-config-preserved/cloud.json.*")]
        assert kept == [corrupt], "the unparseable bytes were destroyed rather than kept"

    def test_the_precondition_is_decided_before_any_preservation(self, tmp_path):
        """Structural, because the ordering is the property and a passing pair of
        behavioural tests would also pass if the preservation merely moved.

        Read from the AST of `_merge_once`: the statement that can return None for the
        precondition must come before the call that moves the file aside.
        """
        import ast as _ast
        import pathlib as _pathlib

        path = _pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew/cloud/config.py"
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        fn = next(
            n
            for n in _ast.walk(tree)
            if isinstance(n, _ast.FunctionDef) and n.name == "_merge_once"
        )
        declines = [
            n.lineno
            for n in _ast.walk(fn)
            if isinstance(n, _ast.If)
            and any(isinstance(b, _ast.Return) for b in n.body)
            and "expect_last_tag" in _ast.dump(n.test)
        ]
        preserves = [
            n.lineno
            for n in _ast.walk(fn)
            if isinstance(n, _ast.Call)
            and isinstance(n.func, _ast.Name)
            and n.func.id == "_preserve_displaced"
        ]
        assert declines and preserves, (
            "_merge_once no longer has both a precondition decline and a preservation, so "
            "this test is measuring nothing"
        )
        assert max(declines) < min(preserves), (
            "the preservation runs before the precondition can decline, so a rejected "
            "update moves the operator's configuration aside and writes nothing"
        )

    def test_the_destroy_path_carries_its_precondition_into_the_write(self):
        """Source-level, because the race needs two processes to show behaviourally.

        Real keyword node from the AST, so the explaining comment beside the call cannot
        satisfy it -- that mistake was already made once on this PR.
        """
        import ast as _ast
        import pathlib as _pathlib

        path = _pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew/cli_cloud.py"
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        guarded = [
            n
            for n in _ast.walk(tree)
            if isinstance(n, _ast.Call)
            and isinstance(n.func, _ast.Attribute)
            and n.func.attr == "apply_update"
            and any(kw.arg == "expect_last_tag" for kw in n.keywords)
        ]
        assert guarded, (
            "cli_cloud clears last_tag without carrying its precondition into the locked "
            "write, so a tag a launch recorded in between can be cleared"
        )

    @pytest.mark.parametrize(
        "kind",
        ["too_deep", "huge_integer", "not_utf8", "not_json", "wrong_shape"],
    )
    def test_a_file_that_cannot_become_a_document_reads_as_defaults(self, tmp_path, kind):
        """One answer for every way a hand-edited file fails to parse.

        These reach `json.loads` through different failure types: a decode error, a
        `JSONDecodeError`, the interpreter's integer-string limit (a plain `ValueError` on
        syntax that is perfectly valid JSON), and a `RecursionError` on depth the parser
        accepts less of than it can build. No caller can act differently on any of them, so
        naming them one at a time is what lets one be missed; this list is the answer.

        Parametrized on a short KEY, not on the payload: pytest exports the node id as an
        environment variable, and a 60,000-bracket document in the id blows past the Windows
        32767-character cap, which the repo has a gate for.
        """
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        if kind == "not_utf8":
            p.write_bytes(b'{"profile": "\xff\xfe"}')
        else:
            raw = {
                "too_deep": '{"fargate": ' + "[" * 60000 + "]" * 60000 + "}",
                "huge_integer": '{"fargate": ' + "9" * 5000 + "}",
                "not_json": "{not json",
                "wrong_shape": "[1, 2, 3]",
            }[kind]
            p.write_text(raw, encoding="utf-8")

        loaded = CloudConfig.load(p)  # must not raise
        assert loaded.profile == "", f"{kind} did not read as defaults"
        assert loaded.fargate is None, f"{kind} retained a block"

    def test_the_lock_symlink_refusal_does_not_depend_on_o_nofollow(self, tmp_path):
        """The same refusal on a platform without ``O_NOFOLLOW``, which is Windows.

        This is the gap a Windows lane found in the first version, and it found it through the
        sibling test above: the flag is 0 there, so the open followed the link and the sibling
        was never judged -- it was judged only when the open FAILED. Held at the Windows value
        here so the platform-independent path is what gets exercised, because a Linux run with
        the real flag passes either way and cannot tell the two apart.
        """
        from kiro_crew.cloud import config as cloud_config
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        outside = tmp_path / "someone-elses-file"
        outside.write_text("untouched", encoding="utf-8")
        lock = tmp_path / "cloud.json.lock"
        lock.symlink_to(outside)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cloud_config, "_O_NOFOLLOW", 0)  # what Windows has
            cfg = CloudConfig.load(p)
            cfg.last_tag = "no-nofollow"
            cfg.save(p)

        assert CloudConfig.load(p).last_tag == "no-nofollow"
        assert not lock.is_symlink(), "without O_NOFOLLOW the link survived, so it was followed"
        assert outside.read_text(encoding="utf-8") == "untouched"

    def test_a_usable_lock_file_is_left_alone(self, tmp_path):
        """The shape check must not churn the ordinary case.

        Checking the shape before the open means it runs on EVERY write, so a lone regular
        file has to survive it -- otherwise every save would delete and recreate the lock.
        """
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        lock = tmp_path / "cloud.json.lock"
        # Identifiable CONTENT, not the inode: an inode comparison can pass by reuse after an
        # unlink, so it cannot tell "spared" from "deleted and recreated". The writer only
        # flocks this file and never truncates it, so surviving bytes are the discriminator.
        lock.write_text("survivor", encoding="utf-8")

        CloudConfig.apply_update(p, last_tag="kept")
        assert lock.is_file()
        assert (
            lock.read_text(encoding="utf-8") == "survivor"
        ), "the ordinary lock file was deleted and recreated on an ordinary write"

    @pytest.mark.parametrize("contents", ["absent", "empty", "complete"])
    def test_a_delegated_workspace_over_the_config_is_refused_whatever_it_holds(
        self, tmp_path, contents
    ):
        """The seal cannot reach a delegated spawn, so the SPAWN is refused instead.

        Parametrized over contents on purpose. Conditioning this on whether a Fargate block
        exists was tried and is wrong: an agent does not need to swap a field it can create,
        so with no block it writes a complete one and the owner's next launch runs its image.
        All three cases must refuse identically, which is what makes the rule content-blind.
        """
        from kiro_crew import sandbox
        from kiro_crew.cloud import config as cloud_config

        home = tmp_path / "crew"
        home.mkdir()
        if contents == "empty":
            (home / "cloud.json").write_text("{}", encoding="utf-8")
        elif contents == "complete":
            (home / "cloud.json").write_text(
                json.dumps({"profile": "p", "region": "us-east-1", "fargate": COMPLETE_FARGATE}),
                encoding="utf-8",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sandbox, "config_dir", lambda: home)
            mp.setattr(cloud_config, "config_dir", lambda: home)
            mp.setattr(sandbox.sys, "platform", "win32")  # a delegated platform
            reason = sandbox.delegated_workspace_exposes_sealed_target(str(tmp_path))

        assert reason is not None, f"a delegated workspace containing the config ({contents})"
        assert "sealed cloud configuration" in reason, reason
        assert "model credential" in reason, "the reason must name the actual consequence"

    def test_a_delegated_workspace_on_the_preserved_directory_is_refused(self, tmp_path):
        """The seal denies the write; the delegated spawn never reaches the seal.

        Sealing the preservation directory without covering it here left exactly the write the
        seal exists to deny -- an agent editing or deleting the only remaining copy -- on the
        platforms that delegate. The guard is the enforcement point on those paths.
        """
        from kiro_crew import sandbox
        from kiro_crew.cloud import config as cloud_config

        home = tmp_path / "crew"
        home.mkdir()
        kept = home / cloud_config._PRESERVED_DIRNAME
        kept.mkdir()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sandbox, "config_dir", lambda: home)
            mp.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
            mp.setattr(sandbox.sys, "platform", "win32")  # a delegated platform
            reason = sandbox.delegated_workspace_exposes_sealed_target(str(kept))

        assert reason is not None, "a delegated workspace set to the preservation directory"
        assert "preserved cloud configurations" in reason, reason
        assert "only remaining copy" in reason, "the reason must name the actual consequence"

    def test_every_strict_leaf_of_either_shape_is_covered_by_the_delegated_guard(self):
        """The omission was shape-shaped, so the pin is on the DERIVATION, not on a member.

        The guard built its targets from the FILE list alone, which covers one shape and leaves
        the other uncovered however many entries are added. Deriving from one mapping keyed by
        both lists is what makes the next sealed leaf covered by construction; this asserts the
        mapping and the two lists cannot drift, in either direction.
        """
        import inspect

        from kiro_crew import sandbox

        both = set(sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES) | set(
            sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
        )
        assert set(sandbox._DELEGATED_OVERLAP_LEAF_REASONS) == both

        # And the guard reads that mapping, rather than one of the two lists it can outlive.
        src = inspect.getsource(sandbox.delegated_workspace_exposes_sealed_target)
        assert "for leaf in _DELEGATED_OVERLAP_LEAF_REASONS" in src, src[:0] or (
            "the target list must be derived from the mapping, or one leaf shape goes uncovered"
        )

    def test_a_delegated_workspace_elsewhere_is_left_alone(self, tmp_path):
        """Only an overlap is refused, so an ordinary project workspace still spawns."""
        from kiro_crew import sandbox

        home = tmp_path / "crew"
        home.mkdir()
        (home / "cloud.json").write_text("{}", encoding="utf-8")
        elsewhere = tmp_path / "some-project"
        elsewhere.mkdir()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sandbox, "config_dir", lambda: home)
            mp.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
            mp.setattr(sandbox.sys, "platform", "win32")
            assert sandbox.delegated_workspace_exposes_sealed_target(str(elsewhere)) is None

    # (kind, bytes) -- every way a PRESENT file can fail to be a record.
    UNUSABLE = [
        ("truncated", b'{"profile": "prod", "region": "eu-west-1"'),
        ("not an object", b"[1, 2, 3]"),
        ("not utf-8", b'\xcb\xff{"profile": "prod"}'),
        ("oversized", b'{"pad": "' + b"x" * (_MAX_FILE_BYTES + 64) + b'"}'),
    ]

    @pytest.mark.parametrize("kind,blob", UNUSABLE, ids=[k for k, _ in UNUSABLE])
    def test_unreadable_bytes_are_moved_aside_not_destroyed(self, tmp_path, kind, blob):
        """Both obvious moves lose something, so this one does neither.

        Writing over the file destroys what it held -- for this file the Fargate block, since
        every caller passes profile and region itself. Refusing to write strands the instance
        the call exists to record. Moving the bytes aside first costs neither.
        """
        p = tmp_path / "cloud.json"
        p.write_bytes(blob)

        out = CloudConfig.apply_update(p, last_tag="kc-20260918-000000-abcd")

        kept = list(tmp_path.glob("cloud-config-preserved/cloud.json.*"))
        assert len(kept) == 1, f"{kind}: expected one preserved copy, got {kept}"
        assert kept[0].read_bytes() == blob, f"{kind}: the operator's bytes were destroyed"
        assert out.last_tag == "kc-20260918-000000-abcd", f"{kind}: the instance was not recorded"
        assert CloudConfig.load(p).last_tag == "kc-20260918-000000-abcd", kind

    def test_a_second_corruption_does_not_destroy_the_first_preserved_copy(self, tmp_path):
        """Two events, two files. A fixed sidecar name would lose the earlier bytes."""
        p = tmp_path / "cloud.json"
        first = b'{"profile": "first", "fargate": {"cluster": "one"}'
        second = b'{"profile": "second", "fargate": {"cluster": "two"}'

        p.write_bytes(first)
        CloudConfig.apply_update(p, last_tag="kc-20260918-000000-aaaa")
        p.write_bytes(second)
        CloudConfig.apply_update(p, last_tag="kc-20260918-000000-bbbb")

        kept = sorted(q.read_bytes() for q in tmp_path.glob("cloud-config-preserved/cloud.json.*"))
        assert kept == sorted([first, second]), [q.name for q in tmp_path.iterdir()]

    def test_the_name_is_claimed_not_merely_checked(self, tmp_path):
        """A look-then-move loses the race it is trying to win, so the claim is the test."""
        import ast as _ast
        import inspect
        import textwrap

        src = inspect.getsource(cloud_config._preserve_displaced)
        tree = _ast.parse(textwrap.dedent(src))
        flags = {
            n.attr
            for n in _ast.walk(tree)
            if isinstance(n, _ast.Attribute) and n.attr.startswith("O_")
        }
        assert {"O_CREAT", "O_EXCL"} <= flags, f"must claim exclusively, got {flags}"
        # AST, not substring: the docstring says "does not exist" and would satisfy a text
        # check while the code did the wrong thing.
        called = {
            n.func.attr
            for n in _ast.walk(tree)
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
        }
        assert "exists" not in called, "an exists() check is the race this avoids"

    def test_a_write_remerges_over_an_edit_that_ignored_the_lock(self, tmp_path):
        """Holding the advisory lock is not knowing nothing else wrote.

        The competing edit lands between the read and the replace. Re-merging keeps BOTH:
        the competitor's field and this caller's.
        """
        p = tmp_path / "cloud.json"
        CloudConfig(profile="orig", region="us-east-1").save(p)
        state = {"n": 0}
        real = cloud_config._raw_bytes_or_none

        def _interleave(path):
            # First call is the witness; the second is the pre-replace check. Between them a
            # competing writer lands, so the check must see a difference and force a re-merge.
            state["n"] += 1
            if state["n"] == 2:
                CloudConfig(profile="orig", region="eu-west-1", last_tag="").save(path)
            return real(path)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cloud_config, "_raw_bytes_or_none", _interleave)
            # The lock is NOT disabled. An advisory lock binds only writers that take it, so
            # the check has to run while it is held -- that is the whole point.
            CloudConfig.apply_update(p, profile="mine")

        after = CloudConfig.load(p)
        assert after.profile == "mine", "this caller's field was lost"
        assert after.region == "eu-west-1", "the competing writer's field was erased"

    def test_the_witness_check_is_not_conditional_on_holding_the_lock(self):
        """Pinned structurally, because the bug was a short-circuit that skipped the check.

        A ``serialized or ...`` in front of the comparison reads as an optimization and is
        actually a hole: it is exactly the case a person editing the file falls into.
        """
        import inspect

        src = inspect.getsource(CloudConfig.apply_update)
        body = src.split("with _writer_lock", 1)[1]
        assert "_raw_bytes_or_none(p) == witness" in body, body[-400:]
        assert "serialized" not in body, "the check must not be guarded by a lock flag"

    def test_a_directory_in_the_way_still_records_the_instance(self, tmp_path):
        """A directory where the config belongs must still leave the deploy recorded.

        Claiming the name first and replacing it is the shape that cannot work: POSIX renames a
        directory onto an EMPTY directory while Windows refuses to replace an existing directory
        at all, so the rename to an unheld name is the claim on both.
        """
        p = tmp_path / "cloud.json"
        p.mkdir()
        (p / "marker").write_text("x", encoding="utf-8")

        out = CloudConfig.apply_update(p, last_tag="kc-20260918-000000-abcd")

        assert out.last_tag == "kc-20260918-000000-abcd", "the instance was not recorded"
        assert CloudConfig.load(p).last_tag == "kc-20260918-000000-abcd"
        kept = [q for q in tmp_path.glob("cloud-config-preserved/cloud.json.*") if q.is_dir()]
        assert len(kept) == 1 and (kept[0] / "marker").exists(), list(tmp_path.iterdir())

    def test_preserving_a_directory_never_replaces_one_already_there(self, tmp_path):
        """The claim must fail rather than overwrite, whichever platform it runs on.

        Same rule as the file shape, and the same reason: what a collision would overwrite is
        another preserved copy.
        """
        p = tmp_path / "cloud.json"
        p.mkdir()
        (p / "marker").write_text("new", encoding="utf-8")

        kept_dir = tmp_path / cloud_config._PRESERVED_DIRNAME
        kept_dir.mkdir()
        taken = kept_dir / "cloud.json.aaaaaaaaaaaaaaaa"
        taken.mkdir()
        (taken / "marker").write_text("earlier", encoding="utf-8")

        with pytest.MonkeyPatch.context() as mp:
            # Every draw returns the one name already taken, so no claim can succeed.
            mp.setattr(cloud_config.secrets, "token_hex", lambda _n: "a" * 16)
            with pytest.raises(OSError, match="left as it is"):
                CloudConfig.apply_update(p, last_tag="kc-20260918-000000-abcd")

        assert (taken / "marker").read_text(
            encoding="utf-8"
        ) == "earlier", "a preserved directory was overwritten by a later one"
        assert (p / "marker").read_text(encoding="utf-8") == "new", "the original was moved"

    def test_the_directory_claim_does_not_depend_on_replacing_a_directory(self):
        """Structural, because no Linux run can exercise what Windows forbids.

        POSIX permits renaming a directory onto an empty directory and Windows does not, so a
        behavioural test on this host passes either way. The pin is therefore that the directory
        path uses ``os.rename`` to an unheld name and never ``os.replace``.
        """
        import ast as _ast
        import inspect
        import textwrap

        src = textwrap.dedent(inspect.getsource(cloud_config._preserve_displaced))
        fn = _ast.parse(src).body[0]
        branch = next(
            n for n in _ast.walk(fn) if isinstance(n, _ast.If) and "is_dir" in _ast.dump(n.test)
        )
        calls = {
            n.func.attr
            for n in _ast.walk(branch)
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
        }
        assert "rename" in calls, calls
        assert (
            "replace" not in calls
        ), "the directory path replaces an existing directory, which Windows refuses"
        assert (
            "mkdir" not in calls
        ), "a pre-claimed directory placeholder is a claim the move cannot honour on Windows"

    def test_the_lock_is_sealed_and_precreated_like_the_config_itself(self):
        """An unsealed lock makes the lock pointless: two writers flock different inodes.

        Pinned against BOTH lists, because a seal without a pre-creation leaves the
        absent-file case -- a name nothing occupies is a name an agent creates.
        """
        from kiro_crew import sandbox

        assert "cloud.json.lock" in sandbox._CREW_READONLY_LEAVES
        assert "cloud.json.lock" in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        # The same pairing the fork-lineage sidecar already has, which is the prior art.
        assert "agent_model_state.json.lock" in sandbox._CREW_READONLY_LEAVES

    def test_the_writer_takes_the_lock_this_seal_covers(self):
        """The seal is worthless if the writer locks some other name."""
        import inspect

        src = inspect.getsource(CloudConfig.apply_update)
        assert 'p.name + ".lock"' in src, src[:0] or "the locked name must be <config>.lock"

    def test_the_preserved_copies_live_somewhere_an_agent_cannot_reach(self, tmp_path):
        """Sealing the config and leaving its only backup writable is the same loss.

        The copies carry random names, so no seal can name them; the seal is therefore on
        their DIRECTORY, pinned against all three lists -- read-only, pre-created so the seal
        has something to bind, and name-pinned so it cannot be aliased out of the way.
        """
        from kiro_crew import sandbox
        from kiro_crew.cloud import config as cloud_config

        d = cloud_config._PRESERVED_DIRNAME
        assert d in sandbox._CREW_READONLY_LEAVES
        assert d in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
        assert d in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES

        # And the writer actually puts them there, rather than beside the config.
        p = tmp_path / "cloud.json"
        p.write_bytes(b"{ unparseable")
        kept = cloud_config._preserve_displaced(p)
        assert kept.parent == tmp_path / d, f"preserved outside the sealed directory: {kept}"
        assert not list(tmp_path.glob("cloud.json.*")), "a copy was left in the writable parent"

    def test_the_exhausted_retry_keeps_the_bytes_it_replaces(self, tmp_path, monkeypatch):
        """The last attempt must not overwrite content newer than the merge it writes.

        A writer that wins every bounded retry is not hypothetical -- it is a person saving in
        an editor, which is why the retry is bounded at all. Writing anyway destroyed their
        newest bytes; refusing would strand the instance being recorded. Keeping the bytes
        first costs neither, so the bound stops being a loss.
        """
        from kiro_crew.cloud import config as cloud_config
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)

        # A competing writer that lands after EVERY read, so no attempt's witness survives
        # and the loop really reaches its bound. Each landing writes different bytes, which
        # is what keeps every comparison unequal.
        landed = []
        real = cloud_config._raw_bytes_or_none

        def moving(path):
            got = real(path)
            if path == p:
                payload = b'{"profile": "edited-by-a-human-%d", "region": "eu-west-1"}' % len(
                    landed
                )
                landed.append(payload)
                p.write_bytes(payload)
            return got

        monkeypatch.setattr(cloud_config, "_raw_bytes_or_none", moving)
        CloudConfig.apply_update(p, last_tag="kc-spent")
        monkeypatch.undo()

        assert CloudConfig.load(p).last_tag == "kc-spent", (
            "the instance was not recorded, so a real deploy is running with nothing "
            "pointing at it"
        )
        kept = [q.read_bytes() for q in tmp_path.glob(f"{cloud_config._PRESERVED_DIRNAME}/*")]
        assert landed[-1] in kept, (
            "the bytes the final write replaced were destroyed; they are the other writer's "
            f"newest content and nothing else holds them. kept={kept} landed={landed[-1]!r}"
        )

    def test_the_preservation_name_cannot_be_occupied_in_advance(self, tmp_path):
        """A predictable name is one a sandboxed process can take before we need it.

        Every name taken turns preserving into refusing, which strands the instance the
        write was recording -- so the name carries random bytes instead of a counter.
        """
        import re

        names = []
        for _ in range(3):
            p = tmp_path / "cloud.json"
            p.write_bytes(b"{truncated")
            names.append(cloud_config._preserve_displaced(p).name)

        assert len(set(names)) == 3, f"names repeat, so they are guessable: {names}"
        for n in names:
            assert re.fullmatch(r"cloud\.json\.[0-9a-f]{16}", n), n

    def test_a_name_that_cannot_be_claimed_leaves_the_file_alone(self, tmp_path):
        """Exhaustion must not fall back to overwriting a copy already preserved."""
        p = tmp_path / "cloud.json"
        p.write_bytes(b"{truncated")
        before = p.read_bytes()
        kept_dir = tmp_path / cloud_config._PRESERVED_DIRNAME
        kept_dir.mkdir()
        taken = kept_dir / "cloud.json.aaaaaaaaaaaaaaaa"
        taken.write_bytes(b"earlier")

        with pytest.MonkeyPatch.context() as mp:
            # Every draw returns the one name already taken, so no claim can succeed.
            mp.setattr(cloud_config.secrets, "token_hex", lambda _n: "a" * 16)
            with pytest.raises(OSError, match="left as it is"):
                CloudConfig.apply_update(p, last_tag="kc-20260918-000000-abcd")

        assert p.read_bytes() == before
        assert taken.read_bytes() == b"earlier", "a preserved copy was overwritten"

    def test_moving_it_aside_is_reported_because_settings_were_lost(self, tmp_path, caplog):
        """Silence here would look like an ordinary write to an operator whose block is gone."""
        p = tmp_path / "cloud.json"
        p.write_bytes(b'{"profile": "prod", "fargate": {"cluster": "c"}')  # truncated
        with caplog.at_level("WARNING"):
            CloudConfig.apply_update(p, last_tag="kc-20260918-000000-abcd")
        assert any(
            cloud_config._PRESERVED_DIRNAME in r.getMessage() for r in caplog.records
        ), caplog.text
        # And it names WHERE, not just a bare filename: the copy sits in the preservation
        # directory rather than beside the config, so a message carrying only the filename
        # sends the operator to the wrong directory.
        assert any(
            f"{cloud_config._PRESERVED_DIRNAME}/cloud.json." in r.getMessage()
            for r in caplog.records
        ), caplog.text

    def test_a_file_that_cannot_even_be_moved_is_left_exactly_as_it_is(self, tmp_path):
        """The one path that does not write: destroying it is worse than not recording."""
        p = tmp_path / "cloud.json"
        p.write_bytes(b"{truncated")
        before = p.read_bytes()

        def _boom(*_a, **_k):
            raise OSError(13, "Permission denied")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cloud_config.os, "replace", _boom)
            with pytest.raises(OSError, match="left alone"):
                CloudConfig.apply_update(p, last_tag="kc-20260918-000000-abcd")
        assert p.read_bytes() == before

    @pytest.mark.parametrize("kind,blob", UNUSABLE, ids=[k for k, _ in UNUSABLE])
    def test_load_still_tolerates_every_one_of_them(self, tmp_path, kind, blob):
        """Two policies over ONE parser: the reader tolerates what the writer refuses."""
        p = tmp_path / "cloud.json"
        p.write_bytes(blob)
        assert CloudConfig.load(p) == CloudConfig(), kind

    def test_an_absent_file_is_written_not_refused(self, tmp_path):
        """Absence is the first run, so it must still write -- the refusal is only for a
        file that EXISTS and cannot be read."""
        p = tmp_path / "sub" / "cloud.json"
        out = CloudConfig.apply_update(p, profile="prod", region="eu-west-1")
        assert p.exists() and out.profile == "prod"
        assert CloudConfig.load(p).region == "eu-west-1"

    def test_a_readable_file_keeps_the_fields_the_caller_did_not_name(self, tmp_path):
        """The merge still merges: refusing the unreadable case cost the normal one nothing."""
        p = tmp_path / "cloud.json"
        CloudConfig(profile="prod", region="eu-west-1", fargate={"cluster": "c"}).save(p)
        CloudConfig.apply_update(p, last_tag="kc-20260918-000000-abcd")
        after = CloudConfig.load(p)
        assert (after.profile, after.region, after.fargate) == (
            "prod",
            "eu-west-1",
            {"cluster": "c"},
        )

    def test_the_set_of_cloud_config_writers_is_closed(self):
        """Show the SET, so a new writer cannot be added without this failing.

        Patching the caller a reviewer happened to name leaves the next one for the next
        reviewer. What closes the class is enumerating every write of this document and
        requiring each to be the field-scoped one.
        """
        import ast as _ast
        import pathlib as _pathlib

        src_root = _pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew"
        # `load` is a read and is always fine. Anything ELSE that is not `apply_update`
        # fails, including a write method added later under a name nobody here foresaw --
        # that is what makes this a closed set rather than a list of the two callers a
        # reviewer happened to name.
        allowed = {"apply_update", "load"}
        offenders: list[str] = []
        for f in sorted(src_root.rglob("*.py")):
            if f.name == "config.py" and f.parent.name == "cloud":
                continue  # the implementation itself
            try:
                tree = _ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for n in _ast.walk(tree):
                if not (isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)):
                    continue
                recv = n.func.value
                is_cloud_cfg = (isinstance(recv, _ast.Name) and recv.id == "CloudConfig") or (
                    isinstance(recv, _ast.Attribute) and recv.attr == "CloudConfig"
                )
                if is_cloud_cfg and n.func.attr not in allowed:
                    offenders.append(f"{f.relative_to(src_root)}:{n.lineno} .{n.func.attr}")
        assert not offenders, (
            "these write CloudConfig by a route other than apply_update: "
            f"{offenders}. Every writer runs after its remote work, so each must name the "
            "fields it owns and must not be refusable."
        )

    def test_a_value_too_deep_to_serialize_is_refused_at_ingest(self, tmp_path):
        """Nesting the parser accepts but the serializer cannot emit is a refusal, not a crash.

        The two limits are independent: `json.loads` builds a document `json.dumps` will not
        write back. And the depth that breaks is not a property of the document, because the
        limit is on total stack -- the same value serializes from a shallow caller and raises
        from a deep one -- so there is no depth to validate against and only attempting it
        can answer. Refused at ingest, where nothing is provisioned yet.
        """
        from kiro_crew.cloud.config import _serialize_record

        depth = 2000
        raw = '{"fargate": ' + "[" * depth + '"x"' + "]" * depth + "}"
        p = tmp_path / "cloud.json"
        p.write_text(raw, encoding="utf-8")

        # Pin the premise: if the PARSER also refused this, the test would pass without
        # ever exercising the serializer, which is the thing under test.
        assert json.loads(raw)["fargate"], "the parser must accept it or this proves nothing"

        assert (
            _serialize_record({"fargate": json.loads(raw)["fargate"]}) is None
        ), "a record that cannot be serialized must report 'does not fit', not raise"
        loaded = CloudConfig.load(p)
        assert loaded.fargate is None, "an unwritable record must read as defaults"

    def test_a_planted_lock_directory_still_records_the_write(self, tmp_path):
        """A planted lock sibling must not turn a save into an untracked instance.

        `save()` runs after the engine has provisioned and is the call that records the new
        instance, so anything that makes it raise converts a plant into a RUNNING instance
        with nothing pointing at it. The sibling holds no data, so it is replaced.
        """
        p = tmp_path / "cloud.json"
        self._seed(p)
        (tmp_path / "cloud.json.lock").mkdir()

        cfg = CloudConfig.load(p)
        cfg.last_tag = "planted"
        cfg.save(p)
        assert CloudConfig.load(p).last_tag == "planted", "the record must reach the file"
        assert (tmp_path / "cloud.json.lock").is_file(), "the sibling is replaced, not left"

    def test_a_symlinked_lock_is_not_followed(self, tmp_path):
        """O_NOFOLLOW, so the lock cannot be aimed at a file outside the data home."""
        p = tmp_path / "cloud.json"
        self._seed(p)
        outside = tmp_path / "someone-elses-file"
        outside.write_text("untouched", encoding="utf-8")
        (tmp_path / "cloud.json.lock").symlink_to(outside)

        cfg = CloudConfig.load(p)
        cfg.last_tag = "aliased"
        cfg.save(p)
        assert CloudConfig.load(p).last_tag == "aliased"
        # The surviving LINK is the evidence, not the target's bytes. Following the link
        # opens the foreign file without truncating it, so its contents are unchanged
        # either way and asserting on them cannot tell the two behaviours apart -- the
        # first version of this test did that and stayed green with O_NOFOLLOW removed.
        # What differs is whether the name is still a link afterwards: refused, it is
        # replaced by a real file; followed, the link remains and keeps aiming the lock
        # at a file outside the data home.
        lock = tmp_path / "cloud.json.lock"
        assert not lock.is_symlink(), "the lock is still a symlink, so it was followed"
        assert lock.is_file(), "the lock must be a real file after refusing the alias"
        assert outside.read_text(encoding="utf-8") == "untouched"

    def test_an_unremovable_lock_still_records_the_write(self, tmp_path):
        """When the sibling cannot even be replaced, the record still lands.

        The guarantee degrades from serialized to DETECTED, which is the right direction:
        the stale-write check still refuses a concurrent edit rather than erasing it, while
        refusing to save at all would reinstate the untracked instance.
        """
        p = tmp_path / "cloud.json"
        self._seed(p)
        lock = tmp_path / "cloud.json.lock"
        lock.mkdir()
        (lock / "occupied").write_text("x", encoding="utf-8")  # rmdir cannot remove it

        cfg = CloudConfig.load(p)
        cfg.last_tag = "unremovable"
        cfg.save(p)
        assert CloudConfig.load(p).last_tag == "unremovable"
        assert lock.is_dir(), "the fixture must still be unremovable or this proves nothing"

    def test_no_cloud_caller_reads_mutates_and_saves(self):
        """Every CloudConfig writer outside this module names the fields it owns.

        A caller that holds a snapshot, mutates it and saves either erases whatever landed
        meanwhile or, once `save()` refuses, raises out of a command that has already done
        its remote work. Both paths that did this ran AFTER the remote change -- the resume
        save and the destroy's tag clear -- so both harms were reachable.

        AST, not a source grep: `wizard.py` has the words `cfg.save()` inside an explaining
        comment, and a grep would count that comment as the defect it warns about.
        """
        import ast as _ast
        import pathlib as _pathlib

        for rel in ("src/kiro_crew/cloud/wizard.py", "src/kiro_crew/cli_cloud.py"):
            path = _pathlib.Path(__file__).resolve().parents[1] / rel
            tree = _ast.parse(path.read_text(encoding="utf-8"))
            offenders = [
                n.lineno
                for n in _ast.walk(tree)
                if isinstance(n, _ast.Call)
                and isinstance(n.func, _ast.Attribute)
                and n.func.attr == "save"
                and isinstance(n.func.value, _ast.Name)
                and n.func.value.id in {"cfg", "config", "cloud_config"}
            ]
            assert not offenders, (
                f"{rel} still saves a whole CloudConfig snapshot at line(s) {offenders}; "
                "use CloudConfig.apply_update(**fields_this_caller_owns)"
            )

    def test_the_refusal_is_one_decision_before_any_platform_branch(self):
        """The refusal must be settled once, above every branch that picks a mechanism.

        Stated per platform, the rule is a rule each branch can be edited out of on its
        own -- and that is what happened: both wrapped launchers had it while the
        delegated spawn, which applies no seal of ours at all, did not. So this asserts
        the SHAPE that makes the omission impossible rather than checking the branches one
        by one: the call exists once, inside the single entry point, and it precedes every
        call that chooses or delegates a mechanism.

        Real CALL nodes from the AST, never a substring: a substring search over the
        source did not bite, because the explanatory comment beside the call also named
        it, so deleting the call left the text present. A comment must not be able to
        satisfy an assertion about behaviour.
        """
        import ast
        import inspect
        import pathlib
        import textwrap

        from kiro_crew import sandbox

        module = ast.parse(pathlib.Path(inspect.getfile(sandbox)).read_text(encoding="utf-8"))
        calls = [
            n
            for n in ast.walk(module)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_refuse_aliased_strict_leaves"
        ]
        assert len(calls) == 1, (
            f"_refuse_aliased_strict_leaves is called {len(calls)} times; one decision "
            "means one call site, or the branches can drift again"
        )

        tree = ast.parse(textwrap.dedent(inspect.getsource(sandbox.wrap_argv)))
        refusal_lines = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_refuse_aliased_strict_leaves"
        ]
        assert refusal_lines, "wrap_argv does not refuse an aliased leaf at all"
        branch_lines = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id
            in {
                "_delegate_to_kiro_internal_sandbox",
                "namespace_argv",
                "sandbox_exec_argv",
                "detect_backend",
            }
        ]
        assert branch_lines, "this asserts nothing if wrap_argv branches to no mechanism"
        assert max(refusal_lines) < min(branch_lines), (
            "the refusal is reached after a mechanism is already chosen, so a branch that "
            "returns first -- the delegated spawn does -- skips it"
        )

    def test_an_aliased_leaf_is_refused_whatever_would_own_isolation(self, tmp_path):
        """End to end through the single entry point, so the shape above has a behavior.

        Reached before any platform branch, which is why one call on Linux CI is evidence
        for the delegated and Windows paths too: none of them has run yet.
        """
        from kiro_crew import sandbox

        home = tmp_path / "crew"
        home.mkdir()
        elsewhere = tmp_path / "attacker.json"
        elsewhere.write_text("{}", encoding="utf-8")
        (home / "cloud.json").symlink_to(elsewhere)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sandbox, "config_dir", lambda: home)
            # Without this the test passes for the wrong reason on a host that is ITSELF
            # inside a Crew sandbox: the nested-sandbox passthrough returns above the
            # refusal, because the outer sandbox already applied it and a child cannot
            # create the alias from in there. Clearing the marker exercises the branch
            # under test rather than that one.
            mp.delenv(sandbox._IN_SANDBOX_MARKER, raising=False)
            with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
                sandbox.wrap_argv(["kiro-cli", "chat"], "strict")
        assert "SYMLINK" in str(caught.value)

    def test_the_shared_refusal_covers_every_strict_leaf(self, tmp_path, monkeypatch):
        """Drives the platform-independent helper directly, on every leaf in the set."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
        assert sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES, "the leaf set must not be empty"

        for leaf in sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES:
            target = tmp_path / leaf
            elsewhere = tmp_path / f"real-{leaf}"
            elsewhere.write_text("{}", encoding="utf-8")
            target.symlink_to(elsewhere)
            with pytest.raises(sandbox.SandboxCeilingUnsealable, match="SYMLINK"):
                sandbox._refuse_aliased_strict_leaves()
            target.unlink()

    def test_a_plain_file_is_accepted(self, tmp_path, monkeypatch):
        """The positive side, so the test above cannot pass by refusing everything."""
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
        for leaf in sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES:
            (tmp_path / leaf).write_text("{}", encoding="utf-8")
        sandbox._refuse_aliased_strict_leaves()  # must not raise

    def test_the_pretty_form_is_preferred_when_it_fits(self, tmp_path):
        """So the compact fallback cannot become the everyday path.

        A human edits this file. Falling back to compact output is for the case where
        pretty would breach the ceiling, not for every save.
        """
        from kiro_crew.cloud.config import CloudConfig

        out = tmp_path / "cloud.json"
        CloudConfig(profile="p", region="us-east-1").save(out)
        assert "\n  " in out.read_text(encoding="utf-8"), "a small record stays indented"

    def test_a_good_credential_beside_a_bad_secret_does_not_register(self):
        """Every reference is validated, not just the first that matches.

        An early return accepted a valid credential ref sitting beside a malformed or
        cross-crew one, and the engine then refused the whole document at launch --
        registering a lane that rejects every launch through it.
        """
        same_crew = [
            "kirocrew/crew/demo/OTHER_KEY",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
            "kirocrew/crew/demo/OTHER_KEY-AbCdEf",
        ]
        cross_crew = [
            "kirocrew/crew/other/OTHER_KEY",
            "arn:aws:secretsmanager:us-east-1:999988887777:secret:"
            "kirocrew/crew/other/OTHER_KEY-AbCdEf",
        ]
        good = {**COMPLETE_FARGATE, "secrets": [list(CREDENTIAL_SECRET), same_crew]}
        assert FargateConfig.from_mapping(good) is not None, "a same-crew sibling is fine"

        for label, bad in (("cross-crew", cross_crew), ("malformed", ["junk", "arn:bogus"])):
            block = {**COMPLETE_FARGATE, "secrets": [list(CREDENTIAL_SECRET), bad]}
            assert FargateConfig.from_mapping(block) is None, label

    @pytest.mark.parametrize("field", ["subnets", "security_groups"])
    @pytest.mark.parametrize("bad", [5, "", None, {"a": 1}])
    def test_one_bad_list_member_voids_the_block(self, field: str, bad: object):
        """All-or-nothing, because filtering silently changed the placement.

        Dropping the bad member launched the task in whichever subnets survived -- a
        placement the operator never wrote. One bad member voids the block, exactly as
        one bad secret entry does.
        """
        block = {**COMPLETE_FARGATE, field: ["subnet-ok", bad]}
        assert FargateConfig.from_mapping(block) is None, f"{field}={bad!r}"

    @pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False)])
    def test_a_boolean_public_ip_flag_is_read_as_written(self, value: bool, expected: bool):
        config = FargateConfig.from_mapping({**COMPLETE_FARGATE, "assign_public_ip": value})
        assert config is not None
        assert config.assign_public_ip is expected

    def test_an_absent_public_ip_flag_defaults_to_false(self):
        assert "assign_public_ip" not in COMPLETE_FARGATE
        config = FargateConfig.from_mapping(COMPLETE_FARGATE)
        assert config is not None
        assert config.assign_public_ip is False

    def test_a_movable_tag_is_refused_here_rather_than_at_launch(self):
        """``taskdef.py`` requires ``<repo>@sha256:<64 hex>``.

        Caught at the file boundary, an operator learns it where they typed it. Left
        to the launch, the same mistake surfaces as a refusal from a lane they were
        offered, with nothing pointing back at ``cloud.json``.
        """
        assert FargateConfig.from_mapping({**COMPLETE_FARGATE, "image": "repo:v1"}) is None

    def test_one_bad_secret_entry_drops_the_whole_block(self):
        """Not just that entry.

        Launching with one fewer secret than the operator wrote starts a task that
        then fails on a missing variable -- which is harder to trace than a lane
        that was never offered.
        """
        two = {**COMPLETE_FARGATE, "secrets": [COMPLETE_FARGATE["secrets"][0], ["broken"]]}
        assert FargateConfig.from_mapping(two) is None

    def test_the_block_survives_a_save_and_load(self, tmp_path):
        p = tmp_path / "cloud.json"
        CloudConfig(profile="dev", fargate=COMPLETE_FARGATE).save(p)
        loaded = CloudConfig.load(p)
        assert loaded.fargate == COMPLETE_FARGATE
        assert loaded.fargate_config() is not None

    def test_an_incomplete_block_survives_an_unrelated_save(self, tmp_path):
        """Load, record a launch, save: the operator's half-written block is intact.

        This object is loaded and re-saved to record ``last_tag`` after an ordinary
        EC2 launch. Were the block judged at load and the judgement written back,
        that save would replace a block the operator is mid-way through editing
        with ``null`` -- an incomplete block must read as ABSENT to the lane and
        still round-trip through the file unchanged.
        """
        p = tmp_path / "cloud.json"
        half_written = {"cluster": "kirocrew-crew-prod", "subnets": ["subnet-a"]}
        p.write_text(
            json.dumps({"profile": "dev", "region": "us-west-2", "fargate": half_written}),
            encoding="utf-8",
        )
        cfg = CloudConfig.load(p)
        assert cfg.fargate_config() is None
        cfg.last_tag = "kc-after-ec2"
        cfg.save(p)
        on_disk = json.loads(p.read_text(encoding="utf-8"))
        assert on_disk["fargate"] == half_written
        assert on_disk["last_tag"] == "kc-after-ec2"
        assert CloudConfig.load(p).fargate_config() is None

    @pytest.mark.parametrize("block", ["a string", 42, ["a", "list"], {"secrets": "nope"}])
    def test_a_malformed_block_also_survives_a_save(self, tmp_path, block: object):
        """Not only an incomplete object: any shape the file held is written back."""
        p = tmp_path / "cloud.json"
        p.write_text(json.dumps({"profile": "dev", "fargate": block}), encoding="utf-8")
        cfg = CloudConfig.load(p)
        assert cfg.fargate_config() is None
        cfg.save(p)
        assert json.loads(p.read_text(encoding="utf-8"))["fargate"] == block

    def test_a_corrupt_block_leaves_the_rest_of_the_config_readable(self, tmp_path):
        """One bad block must not cost the profile and region too."""
        p = tmp_path / "cloud.json"
        p.write_text(
            json.dumps({"profile": "dev", "region": "us-west-2", "fargate": {"cluster": "c"}}),
            encoding="utf-8",
        )
        loaded = CloudConfig.load(p)
        assert loaded.fargate_config() is None
        assert loaded.profile == "dev"
        assert loaded.region == "us-west-2"

    def test_no_secret_value_field_exists_to_write_one_into(self):
        """The file's contract is identifiers only, and the shape enforces it.

        A secret is named by its canonical name and its ARN; the value is fetched by
        the task's execution role before the container starts. A field for a value
        would be the first place someone put one.
        """
        fields = {f.name for f in FargateConfig.__dataclass_fields__.values()}
        for forbidden in ("secret_values", "value", "values", "password", "token"):
            assert forbidden not in fields


class TestCloudConfig:
    def test_defaults(self, tmp_path):
        cfg = CloudConfig.load(tmp_path / "cloud.json")
        assert cfg.profile == ""
        assert cfg.region == DEFAULT_REGION
        assert cfg.last_tag == ""

    def test_roundtrip(self, tmp_path):
        p = tmp_path / "cloud.json"
        cfg = CloudConfig(profile="dev", region="us-west-2", last_tag="kc-abc")
        cfg.save(p)
        loaded = CloudConfig.load(p)
        assert loaded.profile == "dev"
        assert loaded.region == "us-west-2"
        assert loaded.last_tag == "kc-abc"

    def test_over_long_last_tag_sanitized_to_empty(self, tmp_path):
        # A 52-63 char last_tag must be sanitized to "" on load — NOT carried
        # into the resume path where validate_tag (cap 51) would raise. The
        # sanitizer's job is "no last launch", not a crash. Keep _TAG_RE in
        # lockstep with ec2._TAG_RE.
        from kiro_crew.cloud import ec2

        assert ec2._TAG_RE.pattern == r"^[a-zA-Z0-9-]{1,51}$"  # the cap we mirror
        p = tmp_path / "cloud.json"
        p.write_text('{"profile": "dev", "region": "us-east-1", "last_tag": "%s"}' % ("a" * 60))
        cfg = CloudConfig.load(p)
        assert cfg.last_tag == ""  # too long -> dropped, no ValidationError later
        # A malformed-charset tag is likewise dropped.
        p.write_text('{"last_tag": "bad tag!"}')
        assert CloudConfig.load(p).last_tag == ""
        # A valid 51-char tag is kept.
        p.write_text('{"last_tag": "%s"}' % ("k" * 51))
        assert CloudConfig.load(p).last_tag == "k" * 51

    def test_never_stores_credentials(self, tmp_path):
        p = tmp_path / "cloud.json"
        CloudConfig(profile="dev", region="us-east-1", last_tag="t").save(p)
        text = p.read_text(encoding="utf-8")
        # Only profile/region/last_tag — no secret-shaped keys.
        for forbidden in ("secret", "access_key", "aws_access", "token", "password"):
            assert forbidden not in text.lower()

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path):
        p = tmp_path / "cloud.json"
        p.write_text("not json{{{")
        cfg = CloudConfig.load(p)
        assert cfg.region == DEFAULT_REGION

    def test_non_object_json_falls_back_to_defaults(self, tmp_path):
        # Valid JSON that isn't an object ("hello", [1,2], 42, null) must not
        # raise AttributeError out of load() — that would give a raw traceback on
        # every `kirocrew cloud` command (handle_cloud only catches AWS/validation
        # errors). Honor the tolerate-a-corrupt-file promise.
        for body in ('"hello"', "[1, 2, 3]", "42", "null", "true"):
            p = tmp_path / "cloud.json"
            p.write_text(body)
            cfg = CloudConfig.load(p)
            assert cfg.region == DEFAULT_REGION
            assert cfg.profile == ""
            assert cfg.last_tag == ""

    def test_missing_region_coerced_to_default(self, tmp_path):
        p = tmp_path / "cloud.json"
        p.write_text('{"profile": "dev", "region": ""}')
        cfg = CloudConfig.load(p)
        assert cfg.region == DEFAULT_REGION


class TestCloudConfigIsSealedAgainstAgentWrites:
    """``cloud.json`` must not be writable through an agent's file-edit tool.

    The Fargate block made this file an input to a security decision:
    ``fargate.image`` chooses the container a launch runs, and the task's execution
    role delivers the model credential into that container. An agent that could
    rewrite the field could name a digest-pinned image of its own -- the digest rule
    constrains the reference's FORM, not who owns the registry -- while leaving the
    owner's placement and secrets intact, so the owner's next launch would hand the
    credential to an image they never chose.
    """

    #: Spelled out rather than looped from the production tuple: a test derived from
    #: the same list the code reads would keep passing after someone emptied it.
    PATHS = ("~/.kiro/crew/cloud.json", "~/.kirocrew/cloud.json")

    @pytest.mark.parametrize("path", PATHS)
    def test_an_agent_may_not_write_it(self, path: str):
        from kiro_crew.security.paths import is_sensitive_write_path

        assert is_sensitive_write_path(path) is True

    @pytest.mark.parametrize("path", PATHS)
    def test_it_stays_readable(self, path: str):
        """Write-protected, not sensitive: the selector reads it on every request.

        Blocking reads would take the Set-up tab down instead of protecting it.
        """
        from kiro_crew.security.paths import is_sensitive_path

        assert is_sensitive_path(path) is False

    def test_the_predicate_can_still_say_no(self):
        """Positive control, so the two assertions above cannot pass vacuously.

        A sibling path under the same crew home that nothing protects must come back
        writable; without this, a predicate that answered True for everything would
        satisfy this class.
        """
        from kiro_crew.security.paths import is_sensitive_write_path

        assert is_sensitive_write_path("~/.kiro/crew/not-a-protected-leaf.json") is False

    def test_the_gateway_can_still_save_it(self, tmp_path):
        """Sealing is placement, not logic: Kiro Crew's own write is unaffected.

        ``save()`` is how an ordinary EC2 launch records ``last_tag``, so a seal that
        also stopped the gateway would break the lane it exists to protect.
        """
        p = tmp_path / "cloud.json"
        CloudConfig(profile="dev", last_tag="t1").save(p)
        assert CloudConfig.load(p).last_tag == "t1"


class TestSaveRefusesWhatLoadWouldReject:
    """A save that outlives its own reader is silent config loss, so it is refused.

    The raw ``fargate`` block is stored verbatim and ``save()`` re-serializes with
    ``indent=2``, so a hand-minified block that fits under the read ceiling can cross
    it once expanded. Publishing that file would make the next ``load()`` fall back to
    defaults and lose the operator's profile, region and Fargate state with nothing
    said. Refusing keeps the previous, readable file.
    """

    def test_an_ordinary_record_still_saves(self, tmp_path):
        p = tmp_path / "cloud.json"
        CloudConfig(profile="dev", region="us-east-1", last_tag="t1").save(p)
        assert CloudConfig.load(p).profile == "dev"

    def test_a_record_that_would_outgrow_the_read_ceiling_is_refused(self, tmp_path):
        p = tmp_path / "cloud.json"
        CloudConfig(profile="dev").save(p)
        before = p.read_text(encoding="utf-8")
        oversized = CloudConfig(profile="dev", fargate={"pad": "x" * (_MAX_FILE_BYTES + 10)})
        with pytest.raises(ValueError, match="unreadable"):
            oversized.save(p)
        assert p.read_text(encoding="utf-8") == before, "the readable file must survive"

    def test_the_refusal_matches_what_load_enforces(self, tmp_path):
        """The two ceilings are the same constant, not two numbers that can disagree."""
        p = tmp_path / "cloud.json"
        p.write_text("x" * (_MAX_FILE_BYTES + 1), encoding="utf-8")
        assert CloudConfig.load(p).profile == ""


class TestTheSealedCloudConfigNameCannotBeAnAlias:
    """A bind mount seals a link's REFERENT, so the sealed leaf must not be a link.

    Otherwise the lexical name stays replaceable in a writable parent: a sandboxed
    process unlinks it, drops its own file there, and the seal is intact around a name
    that now chooses which image a Fargate launch runs.
    """

    def test_cloud_json_is_on_the_nofollow_file_list(self):
        from kiro_crew import sandbox

        assert "cloud.json" in sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES

    def test_every_nofollow_file_leaf_is_also_precreated(self):
        """Mirrors the assert the directory list carries.

        A nofollow leaf that is not materialised has no seal to protect on a default
        install, so the strict check would guard a name nothing binds.
        """
        from kiro_crew import sandbox

        assert set(sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES) <= set(
            sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        )

    def test_the_list_is_not_everything(self):
        """Positive control: the strict path is opt-in, not applied to every leaf."""
        from kiro_crew import sandbox

        assert set(sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES) - set(
            sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES
        )


class TestTheStrictCloudConfigSealRefusesEveryUncoveredName:
    """A read-only bind seals a MOUNT, not an inode, so two names escape it.

    A symlink leaves the lexical name replaceable; a second hardlink puts an alias
    outside the mount that reaches the same inode. For an ordinary ceiling the codebase
    only WARNS about both, because refusing would break a dotfile manager or a snapshot
    tool. For ``cloud.json`` a write through either name picks the container image a
    Fargate launch runs, and the execution role delivers the model credential into it,
    so the strict leaf refuses where the rest warn.
    """

    #: A block present means an alias could choose the image a launch runs, which is
    #: the only reason this leaf refuses where every other one warns.
    RISKY = '{"profile": "p", "fargate": {"cluster": "c"}}'
    #: No block, so an alias selects nothing: treated like every other ceiling.
    HARMLESS = '{"profile": "p", "region": "us-east-1"}'

    @staticmethod
    def _strict_target(tmp_path):
        from kiro_crew import sandbox

        return tmp_path / sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES[0]

    def test_a_lone_regular_file_is_accepted(self, tmp_path):
        from kiro_crew import sandbox

        p = self._strict_target(tmp_path)
        p.write_text(self.RISKY, encoding="utf-8")
        sandbox._require_real_file_nofollow(str(p))

    def test_an_absent_file_is_accepted(self, tmp_path):
        """Absent is the publish path's job, not this check's."""
        from kiro_crew import sandbox

        sandbox._require_real_file_nofollow(str(self._strict_target(tmp_path)))

    def test_a_symlink_is_refused(self, tmp_path):
        from kiro_crew import sandbox

        real = tmp_path / "elsewhere.json"
        real.write_text(self.RISKY, encoding="utf-8")
        p = self._strict_target(tmp_path)
        p.symlink_to(real)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="SYMLINK"):
            sandbox._require_real_file_nofollow(str(p))

    def test_a_hardlinked_file_is_refused(self, tmp_path):
        """The shape the earlier symlink-only check missed entirely."""
        from kiro_crew import sandbox

        p = self._strict_target(tmp_path)
        p.write_text(self.RISKY, encoding="utf-8")
        (tmp_path / "alias.json").hardlink_to(p)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="hardlink"):
            sandbox._require_real_file_nofollow(str(p))

    def test_a_directory_at_the_name_is_refused(self, tmp_path):
        from kiro_crew import sandbox

        p = self._strict_target(tmp_path)
        p.mkdir()
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="not a regular file"):
            sandbox._require_real_file_nofollow(str(p))

    def test_an_unreadable_alias_is_treated_as_risky(self, tmp_path):
        """Contents are never read now, so even an unreadable alias refuses."""
        from kiro_crew import sandbox

        p = self._strict_target(tmp_path)
        p.write_text(self.RISKY, encoding="utf-8")
        (tmp_path / "alias.json").hardlink_to(p)
        p.chmod(0o000)
        try:
            with pytest.raises(sandbox.SandboxCeilingUnsealable):
                sandbox._require_real_file_nofollow(str(p))
        finally:
            p.chmod(0o600)

    #: Contents that a content-based rule would have judged differently. The refusal must
    #: not vary across them: the agent supplies the contents, so any rule reading them can
    #: be satisfied by writing something else. The escaped spellings are the specific
    #: bypass a byte-level probe had; the no-block and empty rows are the ones an earlier
    #: exemption let through, which is how an agent could write a whole block via the alias.
    ENCODINGS = [
        ("plain block", '{"fargate": {"cluster": "c"}}'),
        ("escaped key", '{"\\u0066argate": {"cluster": "c"}}'),
        ("fully escaped key", '{"\\u0066\\u0061\\u0072\\u0067\\u0061\\u0074\\u0065": {}}'),
        ("no block at all", '{"profile": "p", "region": "us-east-1"}'),
        ("empty object", "{}"),
        ("unparseable", "{not json"),
        ("not an object", '"hello"'),
        ("empty file", ""),
    ]

    @pytest.mark.parametrize(("label", "body"), ENCODINGS)
    def test_a_symlink_is_refused_whatever_the_contents(self, tmp_path, label, body):
        from kiro_crew import sandbox

        real = tmp_path / "elsewhere.json"
        real.write_text(body, encoding="utf-8")
        p = self._strict_target(tmp_path)
        p.symlink_to(real)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="SYMLINK"):
            sandbox._require_real_file_nofollow(str(p))

    @pytest.mark.parametrize(("label", "body"), ENCODINGS)
    def test_a_hardlink_is_refused_whatever_the_contents(self, tmp_path, label, body):
        from kiro_crew import sandbox

        p = self._strict_target(tmp_path)
        p.write_text(body, encoding="utf-8")
        (tmp_path / "alias.json").hardlink_to(p)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="hardlink"):
            sandbox._require_real_file_nofollow(str(p))

    def test_the_rule_reads_no_contents_at_all(self, tmp_path):
        """The property that makes the bypass class empty rather than narrower.

        Any rule deciding on contents can be satisfied by writing different contents,
        and the agent is who writes them. Asserted on the source so a future contents
        check cannot creep back in without failing here.
        """
        import inspect

        from kiro_crew import sandbox

        src = inspect.getsource(sandbox._require_real_file_nofollow)
        for reader in ("open(", "read(", "json.loads", "carries_risk"):
            assert reader not in src, f"the strict refusal must not read contents: {reader}"


class TestTheCredentialGateAgreesWithTheEngine:
    """No spelling may pass this gate and then be refused at launch.

    The gate exists so an incomplete block leaves the lane UNREGISTERED instead of
    registered-and-refusing. A gate that accepts a name the engine rejects recreates
    that exact state from inside the gate, which is what a tail-only check did: both a
    bare ``KIRO_API_KEY`` and a wrong-prefix ``junk/KIRO_API_KEY`` passed here and were
    refused by ``identity.secret_env_name``.
    """

    @staticmethod
    def _with_credential_named(name: str) -> dict:
        return {
            **COMPLETE_FARGATE,
            "secrets": [
                [name, f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{name}-AbCdEf"]
            ],
        }

    @pytest.mark.parametrize(
        "name",
        [
            "KIRO_API_KEY",
            "junk/KIRO_API_KEY",
            "kirocrew/crew/KIRO_API_KEY",
            "kirocrew/crew//KIRO_API_KEY",
            "kirocrew/crew/a/b/KIRO_API_KEY",
            "kirocrew/crew/demo/OTHER_KEY",
        ],
    )
    def test_a_name_the_engine_would_refuse_does_not_register_the_lane(self, name: str):
        assert FargateConfig.from_mapping(self._with_credential_named(name)) is None, name

    def test_a_conforming_name_is_accepted(self):
        block = self._with_credential_named("kirocrew/crew/demo/KIRO_API_KEY")
        assert FargateConfig.from_mapping(block) is not None

    def test_every_name_this_gate_accepts_the_engine_also_accepts(self):
        """The property itself, over a MATRIX, checked against the engine.

        Written as a product rather than a hand-picked list because a hand-picked list
        is what let this defect recur three times: each round fixed the spelling that
        had been reported and left the next one. Any name this module admits must
        survive ``secret_env_name``, which is what the launch path calls, so the two
        halves cannot drift apart without a red here.
        """
        import itertools

        from kiro_crew.cloud.fargate.identity import SecretRef, secret_env_name

        prefixes = ["kirocrew/crew/", "kirocrew/crews/", "junk/", ""]
        crews = [
            "demo",
            "DEMO",
            "De-mo",
            "-demo",
            "demo-",
            "d" * 32,
            "d" * 33,
            "dem_o",
            "d\u00e9",
            "a",
            "a-b",
            "a--b",
            "1",
            "",
            "ab/cd",
        ]
        keys = ["KIRO_API_KEY", "kiro_api_key", "OTHER"]

        accepted = 0
        for prefix, crew, key in itertools.product(prefixes, crews, keys):
            name = f"{prefix}{crew}/{key}"
            if FargateConfig.from_mapping(self._with_credential_named(name)) is None:
                continue
            accepted += 1
            arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{name}-AbCdEf"
            secret_env_name(SecretRef(name=name, arn=arn))
        assert accepted, "the matrix must contain at least one accepted name"

    @pytest.mark.parametrize(
        "crew",
        ["DEMO", "De-mo", "-demo", "demo-", "dem_o", "d\u00e9", "d" * 33, ""],
    )
    def test_a_crew_segment_the_engine_would_refuse_does_not_register(self, crew: str):
        """Each of these passed the earlier truthiness check and died in provisioning."""
        name = f"kirocrew/crew/{crew}/KIRO_API_KEY"
        assert FargateConfig.from_mapping(self._with_credential_named(name)) is None, crew

    @pytest.mark.parametrize("crew", ["demo", "a", "1", "a-b", "a--b", "d" * 32])
    def test_a_conforming_crew_segment_registers(self, crew: str):
        name = f"kirocrew/crew/{crew}/KIRO_API_KEY"
        assert FargateConfig.from_mapping(self._with_credential_named(name)) is not None, crew
