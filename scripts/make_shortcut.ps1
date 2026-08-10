# Creates (or refreshes) the "CallAssist" desktop shortcut.
#
# Besides the usual target/icon, it stamps the shortcut with the same
# AppUserModelID the app sets at runtime (src/chat_app.py). Windows resolves
# the taskbar icon of an AUMID-identified window through a shortcut carrying
# that AUMID — without the stamp the taskbar falls back to the generic icon
# even though the window icon itself is set.
param([Parameter(Mandatory = $true)][string]$ProjectDir)

$ErrorActionPreference = 'Stop'
$AppId = 'FICF0X.CallAssist'

$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'CallAssist.lnk'

# Drop the shortcut left by versions released under the old name.
$oldLnk = Join-Path $desktop 'Copiloto de Llamada.lnk'
if (Test-Path $oldLnk) { Remove-Item $oldLnk -Force }

$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnkPath)
$s.TargetPath = Join-Path $ProjectDir '.venv\Scripts\pythonw.exe'
$s.Arguments = '-m src.chat_app'
$s.WorkingDirectory = $ProjectDir
$s.IconLocation = (Join-Path $ProjectDir 'assets\icon.ico') + ',0'
$s.Save()

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class ShortcutAumid
{
    [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore
    {
        int GetCount(out uint count);
        int GetAt(uint index, IntPtr key);
        int GetValue(ref PropertyKey key, out PropVariant value);
        int SetValue(ref PropertyKey key, ref PropVariant value);
        int Commit();
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PropertyKey { public Guid fmtid; public uint pid; }

    [StructLayout(LayoutKind.Explicit)]
    private struct PropVariant
    {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr pointerValue;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SHGetPropertyStoreFromParsingName(
        string path, IntPtr zone, uint flags, ref Guid iid,
        [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

    public static void Set(string lnk, string appId)
    {
        var iid = new Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99");
        IPropertyStore store;
        int hr = SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2 /* GPS_READWRITE */, ref iid, out store);
        if (hr != 0) Marshal.ThrowExceptionForHR(hr);

        // PKEY_AppUserModel_ID
        var key = new PropertyKey { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
        var value = new PropVariant { vt = 31 /* VT_LPWSTR */, pointerValue = Marshal.StringToCoTaskMemUni(appId) };
        hr = store.SetValue(ref key, ref value);
        if (hr != 0) Marshal.ThrowExceptionForHR(hr);
        store.Commit();
        Marshal.ReleaseComObject(store);
        Marshal.FreeCoTaskMem(value.pointerValue);
    }
}
"@

[ShortcutAumid]::Set($lnkPath, $AppId)
Write-Host "Shortcut ready: $lnkPath"
