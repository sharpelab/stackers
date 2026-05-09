"""Probe the FLIR camera for binning / decimation node support."""
from __future__ import annotations

import PySpin

INTERFACE_NAMES = {
    PySpin.intfIInteger: "Integer",
    PySpin.intfIBoolean: "Boolean",
    PySpin.intfICommand: "Command",
    PySpin.intfIFloat: "Float",
    PySpin.intfIString: "String",
    PySpin.intfICategory: "Category",
    PySpin.intfIEnumeration: "Enumeration",
}


def report_node(nm, name: str) -> None:
    node = nm.GetNode(name)
    if node is None:
        print(f"  {name}: <not in node map>")
        return
    if not PySpin.IsAvailable(node):
        print(f"  {name}: <not available>")
        return

    iface = node.GetPrincipalInterfaceType()
    iname = INTERFACE_NAMES.get(iface, f"<intf={iface}>")
    writable = PySpin.IsWritable(node)
    readable = PySpin.IsReadable(node)

    val = "<unreadable>"
    extra = ""
    try:
        if iface == PySpin.intfIInteger:
            ip = PySpin.CIntegerPtr(node)
            if readable:
                val = ip.GetValue()
                extra = f"  range=[{ip.GetMin()},{ip.GetMax()}]"
        elif iface == PySpin.intfIFloat:
            fp = PySpin.CFloatPtr(node)
            if readable:
                val = fp.GetValue()
                extra = f"  range=[{fp.GetMin():.3f},{fp.GetMax():.3f}]"
        elif iface == PySpin.intfIBoolean:
            bp = PySpin.CBooleanPtr(node)
            if readable:
                val = bp.GetValue()
        elif iface == PySpin.intfIString:
            sp = PySpin.CStringPtr(node)
            if readable:
                val = sp.GetValue()
        elif iface == PySpin.intfIEnumeration:
            ep = PySpin.CEnumerationPtr(node)
            if readable:
                val = ep.GetCurrentEntry().GetSymbolic()
                entries = []
                for e in ep.GetEntries():
                    en = PySpin.CEnumEntryPtr(e)
                    if PySpin.IsAvailable(en) and PySpin.IsReadable(en):
                        entries.append(en.GetSymbolic())
                extra = f"  entries={entries}"
    except Exception as e:
        val = f"<err: {e}>"

    print(f"  {name}: {iname}  current={val}{extra}  writable={writable}")


def main() -> int:
    system = PySpin.System.GetInstance()
    try:
        cams = system.GetCameras()
        if cams.GetSize() == 0:
            print("no cameras enumerated")
            return 1
        cam = cams.GetByIndex(0)
        try:
            cam.Init()
        except PySpin.SpinnakerException as e:
            print(f"cam.Init failed: {e}")
            return 2
        try:
            nm = cam.GetNodeMap()

            print("== Sensor / image-size nodes ==")
            for name in (
                "DeviceModelName", "DeviceVendorName",
                "SensorWidth", "SensorHeight",
                "WidthMax", "HeightMax",
                "Width", "Height",
                "OffsetX", "OffsetY",
                "PixelFormat",
            ):
                report_node(nm, name)

            print()
            print("== Binning nodes ==")
            for name in (
                "BinningSelector",
                "BinningHorizontal", "BinningVertical",
                "BinningHorizontalMode", "BinningVerticalMode",
            ):
                report_node(nm, name)

            print()
            print("== Decimation nodes ==")
            for name in (
                "DecimationSelector",
                "DecimationHorizontal", "DecimationVertical",
            ):
                report_node(nm, name)

            print()
            print("== Probing BinningVertical values ==")
            ip = PySpin.CIntegerPtr(nm.GetNode("BinningVertical"))
            if PySpin.IsAvailable(ip) and PySpin.IsWritable(ip):
                orig = ip.GetValue()
                mn, mx = ip.GetMin(), ip.GetMax()
                print(f"  current={orig}  range=[{mn},{mx}]")
                results = {}
                for v in (1, 2, 4, 8):
                    if v < mn or v > mx:
                        results[v] = "out-of-range"
                        continue
                    try:
                        ip.SetValue(v)
                        # Re-read width/height/framerate after setting binning
                        w = PySpin.CIntegerPtr(nm.GetNode("Width"))
                        h = PySpin.CIntegerPtr(nm.GetNode("Height"))
                        wmax = PySpin.CIntegerPtr(nm.GetNode("WidthMax"))
                        hmax = PySpin.CIntegerPtr(nm.GetNode("HeightMax"))
                        fr_node = nm.GetNode("AcquisitionFrameRate")
                        fr = "?"
                        if fr_node:
                            fr_p = PySpin.CFloatPtr(fr_node)
                            if PySpin.IsReadable(fr_p):
                                fr = f"max={fr_p.GetMax():.2f}"
                        wv = w.GetValue() if PySpin.IsReadable(w) else "?"
                        hv = h.GetValue() if PySpin.IsReadable(h) else "?"
                        wmv = wmax.GetValue() if PySpin.IsReadable(wmax) else "?"
                        hmv = hmax.GetValue() if PySpin.IsReadable(hmax) else "?"
                        results[v] = f"W={wv}/{wmv} H={hv}/{hmv} {fr}"
                    except PySpin.SpinnakerException as e:
                        results[v] = f"set failed: {e}"
                # Restore original
                try:
                    ip.SetValue(orig)
                except Exception as e:
                    print(f"  RESTORE FAILED: {e}")
                for v, r in results.items():
                    print(f"  BinningVertical={v}: {r}")
        finally:
            cam.DeInit()
            del cam
        cams.Clear()
    finally:
        system.ReleaseInstance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
