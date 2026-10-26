from puppet_master import lifecycle, sysinfo

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     6565.
Pages active:                                 248968.
Pages inactive:                               642989.
Pages speculative:                              2943.
Pages throttled:                                   0.
Pages wired down:                             156919.
Pages purgeable:                                9393.
File-backed pages:                            696123.
Anonymous pages:                              198777.
"""


def test_available_counts_free_file_cache_and_purgeable_pages():
    gb = sysinfo.parse_vm_stat(VM_STAT)
    assert gb == (6565 + 696123 + 9393) * 16384 / 1e9
    assert 11.5 < gb < 11.8


def test_inactive_anonymous_pages_are_not_counted_as_available():
    bigger = VM_STAT.replace("642989.", "99999999.")
    assert sysinfo.parse_vm_stat(bigger) == sysinfo.parse_vm_stat(VM_STAT)


def test_unparseable_output_gives_none():
    assert sysinfo.parse_vm_stat("") is None
    assert sysinfo.parse_vm_stat("Pages free: 5.") is None  # no page size, no file-backed


def test_available_gb_is_none_when_vm_stat_fails(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no vm_stat")

    monkeypatch.setattr(sysinfo.subprocess, "run", boom)
    assert sysinfo.available_gb() is None


def test_lifecycle_free_gb_uses_available_memory(monkeypatch):
    monkeypatch.setattr(sysinfo, "available_gb", lambda: 7.5)
    assert lifecycle.free_gb() == 7.5


MEMORY_PRESSURE = """The system has 19327352832 (1179648 pages with a page size of 16384).
System-wide memory free percentage: 79%
"""


def test_parse_memory_pressure_gives_free_gb():
    assert sysinfo.parse_memory_pressure(MEMORY_PRESSURE) == 19327352832 * 79 / 100 / 1e9


def test_parse_memory_pressure_garbage_gives_none():
    assert sysinfo.parse_memory_pressure("nope") is None


def fake_run(monkeypatch, outputs: dict):
    monkeypatch.setattr(sysinfo, "_run", lambda cmd: outputs.get(cmd[0]))


def test_available_gb_takes_the_more_lenient_measurement(monkeypatch):
    fake_run(monkeypatch, {"vm_stat": VM_STAT, "memory_pressure": MEMORY_PRESSURE})
    assert sysinfo.available_gb() == sysinfo.parse_memory_pressure(MEMORY_PRESSURE)  # ~15.3 > 11.7
    low = MEMORY_PRESSURE.replace("79%", "10%")
    fake_run(monkeypatch, {"vm_stat": VM_STAT, "memory_pressure": low})
    assert sysinfo.available_gb() == sysinfo.parse_vm_stat(VM_STAT)  # 11.7 > 1.9


def test_available_gb_uses_whichever_measurement_works(monkeypatch):
    fake_run(monkeypatch, {"vm_stat": VM_STAT})
    assert sysinfo.available_gb() == sysinfo.parse_vm_stat(VM_STAT)
    fake_run(monkeypatch, {"memory_pressure": MEMORY_PRESSURE})
    assert sysinfo.available_gb() == sysinfo.parse_memory_pressure(MEMORY_PRESSURE)
    fake_run(monkeypatch, {})
    assert sysinfo.available_gb() is None
