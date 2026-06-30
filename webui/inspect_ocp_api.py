import inspect
import toolkitpy as tkp

tkp.toolkitpy_init("inspect_ocp_api.py")

name = tkp.enum_sections()[0]
pstat = tkp.Pstat(name)

print("PSTAT:", name)
print("\nOcvCurve object:")
print(tkp.OcvCurve)
print("OcvCurve doc:")
print(tkp.OcvCurve.__doc__)

try:
    print("OcvCurve signature:", inspect.signature(tkp.OcvCurve))
except Exception as exc:
    print("OcvCurve signature unavailable:", exc)

curve = tkp.OcvCurve(pstat, 10000)

print("\nCreated curve:", curve)
print("\nOcvCurve methods:")
for item in dir(curve):
    if not item.startswith("_"):
        print(item)

for method_name in ["run", "running", "acq_data", "stop", "close"]:
    if hasattr(curve, method_name):
        method = getattr(curve, method_name)
        print(f"\n{method_name} doc:")
        print(getattr(method, "__doc__", None))
        try:
            print(f"{method_name} signature:", inspect.signature(method))
        except Exception as exc:
            print(f"{method_name} signature unavailable:", exc)

del curve
del pstat
