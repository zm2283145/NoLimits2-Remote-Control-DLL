"""Compile actual NLVM sources against game fixtures; requires a local .NET SDK.

Only syntax is adapted: imports, Java-like final/String/array-length spellings,
and the NLVM System class. No controller decisions are copied into the runner.
"""
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
output = root / "build/scripted_fixture"
output.mkdir(parents=True, exist_ok=True)
for name in ("PanelBlock", "PanelRoute", "PanelController"):
    source = (root / "scripted" / (name + ".nlvm")).read_text()
    source = source.replace("import com.nolimitscoaster.*;", "using TestHost;")
    source = source.replace("import nlvm.math3d.*;", "")
    source = source.replace("extends Script implements BlockSystemController", ": Script, BlockSystemController")
    source = source.replace("static final", "static readonly").replace("String", "string")
    source = source.replace(".length", ".Length").replace(".equals(", ".Equals(")
    source = source.replace("System.", "NLVMSystem.")
    source = source.replace("NLVMSystem.out.", "NLVMSystem.@out.")
    (output / (name + ".cs")).write_text(source)
shutil.copyfile(Path(__file__).with_name("scripted_fixture.cs"), output / "Fixture.cs")
(output / "Fixture.csproj").write_text('''<Project Sdk="Microsoft.NET.Sdk">
<PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net10.0</TargetFramework>
<NuGetAudit>false</NuGetAudit><EnableNETAnalyzers>false</EnableNETAnalyzers>
</PropertyGroup></Project>''')
(output / "NuGet.Config").write_text('<configuration><packageSources><clear /></packageSources></configuration>')
dotnet = shutil.which("dotnet") or r"C:\Program Files\dotnet\dotnet.exe"
project = str(output / "Fixture.csproj")
restored = subprocess.run([dotnet, "restore", project, "--configfile", str(output / "NuGet.Config")], cwd=output)
if restored.returncode:
    sys.exit(restored.returncode)
sys.exit(subprocess.run([dotnet, "run", "--no-restore", "--project", project], cwd=output).returncode)
