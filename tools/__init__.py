"""
tools/__init__.py — Exports ALL_TOOLS for reevaluate-preconditions.
"""

from tools.general import save_step_report, get_workflow_status
from tools.parsing_tools import parse_predicted_conditions, parse_manifest
from tools.evaluation_tools import evaluate_specifications, view_parsed_data
from tools.output_tools import generate_final_output

GENERAL_TOOLS = [save_step_report, get_workflow_status]

STEP_TOOLS = {
    "STEP_00": [parse_predicted_conditions, parse_manifest],
    "STEP_01": [view_parsed_data, evaluate_specifications],
    "STEP_02": [generate_final_output],
}

ALL_TOOLS = list(
    {t.name: t for t in GENERAL_TOOLS + [t for step in STEP_TOOLS.values() for t in step]}.values()
)
