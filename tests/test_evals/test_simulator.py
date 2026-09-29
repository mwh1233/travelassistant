"""User simulators: scripted (PR-safe) and adversarial perturbation."""

from __future__ import annotations

import pytest

from evals.simulator import (
    PERTURBATIONS,
    SIMULATOR_VERSION,
    AdversarialUserSimulator,
    ScriptedUserSimulator,
    UserPersona,
    build_simulator,
)


@pytest.mark.asyncio
async def test_scripted_simulator_walks_its_script():
    sim = ScriptedUserSimulator(turns=["你好", "我要去西安", "谢谢"])

    assert await sim.opening() == "你好"
    assert await sim.reply(1, []) == "我要去西安"
    assert await sim.reply(2, []) == "谢谢"
    assert await sim.reply(3, []) is None


@pytest.mark.asyncio
async def test_scripted_simulator_empty_is_safe():
    sim = ScriptedUserSimulator(turns=[])

    assert await sim.opening() is None


def test_scripted_version_is_exported():
    sim = ScriptedUserSimulator(turns=["x"])

    assert sim.version == SIMULATOR_VERSION
    assert sim.name == "scripted"


@pytest.mark.asyncio
async def test_adversarial_simulator_perturbs_a_declared_turn():
    sim = AdversarialUserSimulator(
        turns=["我要去西安", "行程定了"],
        perturbations={2: "reverse"},
        persona=UserPersona(hidden_goal="去西安"),
    )

    first = await sim.opening()
    second = await sim.reply(1, [first or ""])

    assert first == "我要去西安"  # turn 1 is untouched
    assert second is not None
    assert "反过来" in second or "说错了" in second  # turn 2 is perturbed


@pytest.mark.asyncio
async def test_adversarial_simulator_without_schedule_is_passthrough():
    sim = AdversarialUserSimulator(turns=["我要去西安"])

    first = await sim.opening()
    second = await sim.reply(1, [first or ""])

    assert first == "我要去西安"
    assert second is None  # script exhausted


def test_perturbations_are_a_closed_set():
    assert set(PERTURBATIONS) == {"restate", "reverse", "withdraw", "interrupt", "emotive"}


def test_build_simulator_defaults_to_scripted_and_parses_persona():
    sim = build_simulator(
        {"turns": ["a", "b"], "persona": {"hidden_goal": "省钱", "constraints": ["selected_transport==train"]}}
    )

    assert isinstance(sim, ScriptedUserSimulator)
    assert sim.persona.hidden_goal == "省钱"
    assert sim.persona.constraints == ["selected_transport==train"]
    assert sim.turns == ["a", "b"]


def test_build_simulator_adversarial_kind_parses_schedule():
    sim = build_simulator({"kind": "adversarial", "turns": ["x"], "perturbations": {"1": "withdraw"}})

    assert isinstance(sim, AdversarialUserSimulator)
    assert sim.perturbations == {1: "withdraw"}
