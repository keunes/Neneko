import subprocess
import sys
import zipfile


def test_wheel_contains_data_files(tmp_path):
    """The Kobo resource list must be installed with the package, not only present in the source tree."""
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q",
                    "-w", str(tmp_path), "."], check=True)
    wheel = next(tmp_path.glob("neneko-*.whl"))
    assert "neneko/kobo/native_resources.json" in zipfile.ZipFile(wheel).namelist()
