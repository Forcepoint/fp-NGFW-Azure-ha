#!/usr/bin/env python3

import base64
import sys

SCRIPT_START = """#!/usr/bin/env python3

import os
import base64
import logging
import subprocess
import shutil
import sys

BASE64DATA = \"\"\""""

SCRIPT_END = """
\"\"\"

decoded = base64.b64decode(BASE64DATA)

DEST_SCRIPT_FILE = "/data/run-at-boot"
log_file = "/data/diagnostics/cloud-ha-install.log"

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
    filename=log_file,
    level=logging.INFO
)

def main():
    try:
        logging.info("Stop user_hook")
        subprocess.call(["/bin/msvc", "-D", "user_hook"])
        allow_file = os.path.realpath(__file__) + "_allow"

        with open(allow_file, "r") as fp:
            for line in fp.readlines():
                key, value = line.lower().split(":", 1)
                if key.strip() == "uninstall" and value.strip() == "true":
                    os.remove(DEST_SCRIPT_FILE)
                    os.remove(DEST_SCRIPT_FILE + "_allow")
                    logging.info("run-at-boot script uninstalled")
                    return 0

        logging.info("Create %s", DEST_SCRIPT_FILE)
        with open(DEST_SCRIPT_FILE, "wb+") as fp:
            fp.write(decoded)

        os.chmod(DEST_SCRIPT_FILE, 0o755);
        logging.info("Create %s", DEST_SCRIPT_FILE + "_allow")
        shutil.copy2(allow_file, DEST_SCRIPT_FILE + "_allow")

        logging.info("Start user_hook")
        subprocess.check_call(["/bin/msvc", "-u", "user_hook"])
        logging.info("run-at-boot script installed")
    except:
        logging.error("policy applied script installation failed", exc_info=True)
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())

"""


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <zip_file_name>", file=sys.stderr)
        sys.exit(1)

    zip_file_name = sys.argv[1]

    with open(zip_file_name, "rb") as content_file:
        content = content_file.read()

    encoded_content = base64.b64encode(content)
    SCP = SCRIPT_START + encoded_content.decode("ascii") + SCRIPT_END

    print(SCP)


if __name__ == "__main__":
    main()
