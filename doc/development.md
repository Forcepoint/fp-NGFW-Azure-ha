## Development

Building from source is only recommended if you want to modify the behaviour.
Use prebuilt Github releases otherwise.

### requirements

The script is developed for Python 3.11, so you need to have pyenv installed (
or any other tool such as asdf).

Check Python version::

```
$ python --version
Python 3.11.11
```

### deployment file

The script is delivered as a self-expanding zipapp file:

 - *zipapp* is used to make a Python executable zip file (sometimes found with
   extension .pyz), which is installed as a `run-at-boot` file on engine.
 - The script *utils/generate-script-installer.py* takes this appzip file and
   generates a python program *dist/ha_script_installer.py*. It contains the
   zip as a BASE64 string and a function to extract it to the proper place.

This *dist/ha_script_installer.py* needs to be provided to the customer to be
placed in the custom property 'se_script_path'
(see [user guide](./doc/user_guide.md) for details).

*dist/ha_script_installer.py* will also attempt to copy a file
*ha_script_installer.py_allow* to the installation target.

The file *ha_script_installer.py_allow* is generated automatically
by the SMC custom properties mechanism: see [here](http://help.stonesoft.com/onlinehelp/StoneGate/SMC/6.10.0/GUID-1FB9FE34-59C9-43D9-859B-C98312C172E6.html?hl=_allow)

### release

The following steps must be done when releasing the new version of the script:

1. Change version in `src/ha_script/script.py`, e.g.:
   ```
   __VERSION__ = "1.0.1"
   ```
2. Run tests, build documents and deliverables.
   ```
   make all
   ```
3. Create new branch and commit the changes:
   ```
   git checkout wip/new-branch
   git add *
   git commit
   git push
   ```
4. Create pull-request and wait until the pull-request is approved.
5. Go to GitHub page and click *Create new release*.
6. Create new tag, e.g. `v1.0.1`.
7. Type *Release title* and *Description*.
8. Upload the deliveries.
  - `dist/ha_script_installer.py`
  - `doc/user_guide.pdf`
