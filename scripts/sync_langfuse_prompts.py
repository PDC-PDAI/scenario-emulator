"""Publish the local Front A prompts to Langfuse under LANGFUSE_SYNC_LABEL."""

import argparse

from src.prompts.sync import sync_prompts

if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(sync_prompts())
