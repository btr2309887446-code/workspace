"""
llm_client.py
LLM 调用客户端，支持 OpenAI 兼容 API。
通过环境变量配置：
  - LLM_API_KEY: API 密钥（必需）
  - LLM_BASE_URL: API 端点（默认 https://api.openai.com/v1）
  - LLM_MODEL: 模型名（默认 gpt-4o）
  - LLM_MAX_TOKENS: 最大输出 token（默认 4096）
  - LLM_TEMPERATURE: 温度（默认 0.3）
"""
from __future__ import annotations
import json
import os
import time
from typing import Any

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o"
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TEMPERATURE = 0.3
_MAX_RETRIES = 3
_RETRY_DELAY = 2.0


def _get_client():
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "openai package is required for LLM calls. Install with: pip install openai"
        )

    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_API_KEY environment variable is not set")

    base_url = os.environ.get("LLM_BASE_URL", _DEFAULT_BASE_URL)
    return OpenAI(api_key=api_key, base_url=base_url)


def _get_config() -> dict:
    return {
        "model": os.environ.get("LLM_MODEL", _DEFAULT_MODEL),
        "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", str(_DEFAULT_MAX_TOKENS))),
        "temperature": float(os.environ.get("LLM_TEMPERATURE", str(_DEFAULT_TEMPERATURE))),
    }


def chat(system_prompt: str, user_prompt: str, response_format: dict | None = None) -> str:
    """调用 LLM 并返回响应文本。支持 JSON mode。"""
    client = _get_client()
    cfg = _get_config()

    kwargs: dict[str, Any] = {
        "model": cfg["model"],
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    if response_format is not None:
        kwargs["response_format"] = response_format

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            return content
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                wait = _RETRY_DELAY * attempt
                print(f"[llm] attempt {attempt} failed: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[llm] all {_MAX_RETRIES} attempts failed: {e}")

    raise RuntimeError(f"LLM call failed after {_MAX_RETRIES} retries: {last_error}")


def chat_json(system_prompt: str, user_prompt: str) -> Any:
    """调用 LLM 并返回解析后的 JSON 对象。"""
    client = _get_client()
    cfg = _get_config()

    kwargs: dict[str, Any] = {
        "model": cfg["model"],
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or "{}"
            return json.loads(content)
        except json.JSONDecodeError as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                wait = _RETRY_DELAY * attempt
                print(f"[llm] JSON parse failed attempt {attempt}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[llm] JSON parse failed all attempts: {e}")
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                wait = _RETRY_DELAY * attempt
                print(f"[llm] attempt {attempt} failed: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[llm] all {_MAX_RETRIES} attempts failed: {e}")

    raise RuntimeError(f"LLM JSON call failed after {_MAX_RETRIES} retries: {last_error}")


ANNOTATION_SYSTEM_PROMPT = """You are an expert physics competition problem annotator. Your task is to analyze physics competition problems and provide structured annotations in English.

For each sub-question, you must provide:
1. related_knowledge_points: An array of 3-level knowledge point classifications. Each entry is [Level1, Level2, Level3]. It must strictly follow the IPhO Syllabus Section 2: Theoretical Skills only.
2. physical_model: The physical model(s) used in this problem (e.g., "Two-body gravitational system in rotating reference frame").
3. physical_scenario: The physical scenario described (e.g., "Electrodynamic tether orbiting Earth in the equatorial plane").
4. explicit_conditions: Explicit conditions directly given in the problem text.
5. implicit_conditions: Implicit conditions that must be inferred, each as {"condition_text": "original text from problem", "hidden_meaning": "what this implies"}.
6. grading_rubric: Array of grading criteria strings extracted from the marking scheme.
7. core_idea: The core solution approach in 1-2 sentences.
8. difficulty: One of "easy", "medium", or "hard".
9. answer_type: One of "expression", "numerical", "choice", "equation", "open-ended", or "inequality".

Rules:
- ALL output must be in English.
- Be precise and concise.
- For related_knowledge_points, use only IPhO Syllabus Section 2: Theoretical Skills.
- Do not use Section 3 Experimental skills or Section 4 Mathematics.
- Each knowledge point must be a 3-item array: [Level1, Level2, Level3].
- Level1 must be exactly one of: "Mechanics", "Electromagnetic fields", "Oscillations and waves", "Relativity", "Quantum Physics", "Thermodynamics and statistical physics".
- Level2 must exactly match one of the IPhO Section 2 subcategory names (see examples below).
- Level3 must be the EXACT full text of an IPhO Section 2 leaf-node topic. COPY the topic text VERBATIM from the reference list below — do NOT paraphrase, abbreviate, or invent your own wording.

=== COMPLETE Level 3 TOPIC REFERENCE (IPhO Section 2: Theoretical Skills) ===

Mechanics > Kinematics:
  "Velocity and acceleration of a point particle as the derivatives of its displacement vector"
  "Linear speed"
  "Centripetal and tangential acceleration"
  "Motion of a point particle with a constant acceleration"
  "Addition of velocities and angular velocities"
  "Addition of accelerations without the Coriolis term"
  "Recognition of the cases when the Coriolis acceleration is zero"
  "Motion of a rigid body as a rotation around an instantaneous centre of rotation"
  "Velocities and accelerations of the material points of rigid rotating bodies"

Mechanics > Statics:
  "Finding the centre of mass of a system via summation or via integration"
  "Equilibrium conditions: force balance (vectorially or in terms of projections)"
  "Torque balance (only for one- and two-dimensional geometry)"
  "Normal force"
  "Tension force"
  "Static and kinetic friction force"
  "Hooke's law"
  "Stress"
  "Strain"
  "Young modulus"
  "Stable and unstable equilibria"

Mechanics > Dynamics:
  "Newton's second law (in vector form and via projections (components))"
  "Kinetic energy for translational and rotational motions"
  "Potential energy for simple force fields (also as a line integral of the force field)"
  "Momentum"
  "Angular momentum"
  "Energy"
  "Their conservation laws"
  "Mechanical work and power"
  "Dissipation due to friction"
  "Inertial and non-inertial frames of reference"
  "Inertial force"
  "Centrifugal force"
  "Potential energy in a rotating frame"
  "Moment of inertia for simple bodies (ring, disk, sphere, hollow sphere, rod)"
  "Parallel axis theorem"
  "Finding a moment of inertia via integration"

Mechanics > Celestial mechanics:
  "Law of gravity"
  "Gravitational potential"
  "Kepler's laws (no derivation needed for first and third law)"
  "Energy of a point mass on an elliptical orbit"

Mechanics > Hydrodynamics:
  "Pressure"
  "Buoyancy"
  "Continuity law"
  "Bernoulli equation"
  "Surface tension and the associated energy"
  "Capillary pressure"

Electromagnetic fields > Basic concepts:
  "Concepts of charge and current"
  "Charge conservation"
  "Kirchhoff's current law"
  "Coulomb force"
  "Electrostatic field as a potential field"
  "Kirchhoff's voltage law"
  "Magnetic B-field"
  "Lorentz force"
  "Ampère's force"
  "Biot-Savart law"
  "B-field on the axis of a circular current loop"
  "B-field for simple symmetric systems like straight wire, circular loop and long solenoid"

Electromagnetic fields > Integral forms of Maxwell's equations:
  "Gauss' law (for E- and B-fields)"
  "Ampère's law (with Maxwell correction)"
  "Faraday's law"
  "Using these laws for the calculation of fields when the integrand is almost piece-wise constant"
  "Boundary conditions for the electric field (or electrostatic potential) at the surface of conductors and at infinity"
  "Concept of grounded conductors"
  "Superposition principle for electric and magnetic fields"
  "Uniqueness of solution to well-posed problems"
  "Method of image charges"

Electromagnetic fields > Interaction of matter with electric and magnetic fields:
  "Resistivity and conductivity"
  "Differential form of Ohm's law"
  "Dielectric and magnetic permeability"
  "Relative permittivity and permeability of electric and magnetic materials"
  "Energy density of electric and magnetic fields"
  "Ferromagnetic materials"
  "Hysteresis and dissipation"
  "Eddy currents"
  "Lenz's law"
  "Charges in magnetic field: helicoidal motion"
  "Cyclotron frequency"
  "Drift in crossed E and B fields"
  "Energy of a magnetic dipole in a magnetic field"
  "Dipole moment of a current loop"

Electromagnetic fields > Circuits:
  "Linear resistors and Ohm's law"
  "Joule's law"
  "Work done by an electromotive force"
  "Ideal and non-ideal batteries"
  "Constant current sources"
  "Ammeters"
  "Voltmeters"
  "Ohmmeters"
  "Nonlinear elements of given V-I characteristic"
  "Capacitors and capacitance (also for a single electrode with respect to infinity)"
  "Self-induction and inductance"
  "Energy of capacitors and inductors"
  "Mutual inductance"
  "Time constants for RL and RC circuits"
  "AC circuits: complex amplitude"
  "Impedance of resistors, inductors, capacitors, and combination circuits"
  "Phasor diagrams"
  "Current and voltage resonance"
  "Active power"

Oscillations and waves > Single oscillator:
  "Harmonic oscillations: equation of motion"
  "Frequency"
  "Angular frequency"
  "Period"
  "Physical pendulum and its reduced length"
  "Behaviour near unstable equilibria"
  "Exponential decay of damped oscillations"
  "Resonance of sinusoidally forced oscillators: amplitude of steady state oscillations"
  "Phase shift of steady state oscillations"
  "Free oscillations of LC circuits"
  "Mechano-electrical analogy"
  "Positive feedback as a source of instability"
  "Generation of sine waves by feedback in a LC-resonator"

Oscillations and waves > Waves:
  "Propagation of harmonic waves: phase as a linear function of space and time"
  "Wavelength"
  "Wave vector"
  "Phase and group velocities"
  "Exponential decay for waves propagating in dissipative media"
  "Transverse and longitudinal waves"
  "The classical Doppler effect"
  "Waves in inhomogeneous media: Fermat's principle"
  "Snell's law"
  "Sound waves: speed as a function of pressure (Young's or bulk modulus) and density"
  "Mach cone"
  "Energy carried by waves: proportionality to the square of the amplitude"
  "Continuity of the energy flux"

Oscillations and waves > Interference and diffraction:
  "Superposition of waves: coherence"
  "Beats"
  "Standing waves"
  "Huygens' principle"
  "Interference due to thin films"
  "Diffraction from one and two slits"
  "Diffraction grating"
  "Bragg reflection"

Oscillations and waves > Interaction of electromagnetic waves with matter:
  "Dependence of electric permittivity on frequency (qualitatively)"
  "Refractive index"
  "Dispersion and dissipation of electromagnetic waves in transparent and opaque materials"
  "Linear polarisation"
  "Brewster angle"
  "Polarisers"
  "Malus' law"

Oscillations and waves > Geometrical optics and photometry:
  "Approximation of geometrical optics: rays and optical images"
  "A partial shadow and full shadow"
  "Thin lens approximation"
  "Construction of images created by ideal thin lenses"
  "Thin lens equation"
  "Luminous flux and its continuity"
  "Illuminance"
  "Luminous intensity"

Oscillations and waves > Optical devices:
  "Telescopes and microscopes: magnification and resolving power"
  "Diffraction grating and its resolving power"
  "Interferometers"

Relativity > Relativity:
  "Principle of relativity"
  "Lorentz transformations for the time and spatial coordinate"
  "Lorentz transformations for the energy and momentum"
  "Mass-energy equivalence"
  "Invariance of the spacetime interval and of the rest mass"
  "Addition of parallel velocities"
  "Time dilation"
  "Length contraction"
  "Relativity of simultaneity"
  "Energy and momentum of photons"
  "Relativistic Doppler effect"
  "Relativistic equation of motion"
  "Conservation of energy and momentum for elastic and non-elastic interaction of particles"

Quantum Physics > Probability waves:
  "Particles as waves: relationship between the frequency and energy"
  "Relationship between the wave vector and momentum"
  "Energy levels of hydrogen-like atoms (circular orbits only)"
  "Energy levels of parabolic potentials"
  "Quantization of angular momentum"
  "Uncertainty principle for the conjugate pairs of time and energy"
  "Uncertainty principle for the coordinate and momentum"

Quantum Physics > Structure of matter:
  "Emission and absorption spectra for hydrogen-like atoms"
  "Spectra for other atoms (qualitatively)"
  "Spectra for molecules due to molecular oscillations"
  "Spectral width"
  "Lifetime of excited states"
  "Pauli exclusion principle for Fermi particles"
  "Particles (knowledge of charge and spin): electrons"
  "Electron neutrinos"
  "Protons"
  "Neutrons"
  "Photons"
  "Compton scattering"
  "Protons and neutrons as compound particles"
  "Atomic nuclei"
  "Energy levels of nuclei (qualitatively)"
  "Alpha-, beta- and gamma-decays"
  "Fission"
  "Fusion"
  "Neutron capture"
  "Mass defect"
  "Half life"
  "Exponential decay"
  "Photoelectric effect"

Thermodynamics and statistical physics > Classical thermodynamics:
  "Concepts of thermal equilibrium and reversible processes"
  "Internal energy"
  "Work"
  "Heat"
  "Kelvin's temperature scale"
  "Entropy"
  "Open, closed, isolated systems"
  "First and second laws of thermodynamics"
  "Kinetic theory of ideal gases: Avogadro number"
  "Boltzmann factor"
  "Gas constant"
  "Translational motion of molecules and pressure"
  "Ideal gas law"
  "Translational, rotational and oscillatory degrees of freedom"
  "Equipartition theorem"
  "Internal energy of ideal gases"
  "Root-mean-square speed of molecules"
  "Isothermal, isobaric, isochoric, and adiabatic processes"
  "Specific heat for isobaric and isochoric processes"
  "Forward and reverse Carnot cycle on ideal gas and its efficiency"
  "Efficiency of non-ideal heat engines"

Thermodynamics and statistical physics > Heat transfer and phase transitions:
  "Phase transitions (boiling, evaporation, melting, sublimation)"
  "Latent heat"
  "Saturated vapour pressure"
  "Relative humidity"
  "Boiling"
  "Dalton's law"
  "Concept of heat conductivity"
  "Continuity of heat flux"

Thermodynamics and statistical physics > Statistical physics:
  "Planck's law (explained qualitatively, does not need to be remembered)"
  "Wien's displacement law"
  "Stefan-Boltzmann law"

=== END OF COMPLETE TOPIC REFERENCE ===

CRITICAL RULES for related_knowledge_points:
1. COPY the Level3 text EXACTLY as shown above. Do NOT reword, shorten, or invent topic names.
   Example: if a sub-question involves Lorentz force on a current-carrying wire, emit
   ["Electromagnetic fields", "Basic concepts", "Lorentz force"] — NOT
   "Electromagnetic force on a conductor" or "Lorentz force on a wire".
2. Cross-domain coverage is REQUIRED. A single sub-question often touches several
   IPhO domains (e.g. an "electrodynamic tether" problem mixes Mechanics + Electromagnetic
   fields + Thermodynamics). Output 3–8 entries spanning ALL relevant domains; do NOT
   restrict yourself to a single Level1 just because the problem looks "mostly mechanics".
3. Whenever the solution invokes any of: orbit/Kepler, Newton's laws, energy/momentum
   conservation, EMF/Lorentz/Ampère, Joule heating, Stefan-Boltzmann radiation,
   ideal-gas / Carnot, photon/photoelectric, time dilation/length contraction, lens/mirror,
   Snell/diffraction/polarisation — emit the corresponding Level3 explicitly.
4. Prefer the most specific leaf. If both "Energy" (generic) and "Kinetic energy for
   translational and rotational motions" apply, choose the latter.
5. NEVER emit a Level1 / Level2 / Level3 string that is not literally present in the
   reference list above.

- For grading rubric, extract the actual point allocations and criteria.
- If unsure about a field, make your best educated guess as a physics expert."""


def annotate_subquestion(
    sub_id: str,
    question_text: str,
    solution_text: str,
    grading_rubric_text: str,
    background_text: str,
) -> dict:
    """Call LLM to annotate a single subquestion. Returns a dict with annotation fields."""
    user_prompt = f"""Analyze the following physics competition sub-question and provide annotations.

=== SUB-QUESTION ID ===
{sub_id}

=== BACKGROUND / CONTEXT ===
{background_text[:3000]}

=== QUESTION TEXT ===
{question_text[:3000]}

=== STANDARD SOLUTION ===
{solution_text[:4000]}

=== GRADING RUBRIC (if available) ===
{grading_rubric_text[:4000]}

Provide a JSON object with these fields:
{{
  "related_knowledge_points": [["Level1", "Level2", "Level3"], ...],
  "physical_model": "string describing the physical model(s) used",
  "physical_scenario": "string describing the physical scenario",
  "explicit_conditions": ["condition 1", "condition 2", ...],
  "implicit_conditions": [
    {{"condition_text": "original text from problem", "hidden_meaning": "the implied condition"}},
    ...
  ],
  "grading_rubric": ["Award X pt if ...", ...],
  "core_idea": "core solution approach in 1-2 sentences",
  "difficulty": "easy" | "medium" | "hard",
  "answer_type": "expression" | "numerical" | "choice" | "equation" | "open-ended" | "inequality"
}}"""

    try:
        result = chat_json(ANNOTATION_SYSTEM_PROMPT, user_prompt)
        return result
    except Exception as e:
        print(f"[llm] annotation failed for {sub_id}: {e}")
        return {}
