import inspect
import toolkitpy as tkp

tkp.toolkitpy_init("inspect_ocp_stop.py")

pstat = tkp.Pstat(tkp.enum_sections()[0])
curve = tkp.OcvCurve(pstat, 10000)

for method_name in ["set_stop_adv_max", "set_stop_adv_min", "run", "running", "acq_data", "last_data_point", "count"]:
    method = getattr(curve, method_name)
    print("\n---", method_name, "---")
    print("doc:", getattr(method, "__doc__", None))
    try:
        print("signature:", inspect.signature(method))
    except Exception as exc:
        print("signature unavailable:", exc)

try:
    curve.free()
except Exception:
    pass
