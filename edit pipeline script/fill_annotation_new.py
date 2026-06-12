"""
fill_annotation_new.py
读取 work/Q{n}.subquestions.json 并填充成最终模板格式的 JSON 骨架。
支持通过 LLM 自动填充标注字段；LLM 调用由环境变量 LLM_API_KEY 控制，
若未设置则仅生成骨架（需人工填充）。

新格式：数组形式，每个元素代表一个子问题，完全对齐最终模板.json。
"""
from __future__ import annotations
import json
import os
import re
import sys
from difflib import get_close_matches
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _normalize_latex(text: str) -> str:
    """确保文本中所有数学公式都用 LaTeX $...$ 包裹。
    修复常见问题：双句号、裸变量表达式等。"""
    if not text:
        return text
    # 修复双句号 ".. Otherwise" -> ". Otherwise"
    text = re.sub(r'\.\.\s+Otherwise', '. Otherwise', text)
    # 修复 $.. Otherwise -> $. Otherwise
    text = re.sub(r'\$\.\.\s+Otherwise', r'\$. Otherwise', text)
    return text


def clean_final_answer(raw: str) -> str:
    """清理最终答案，去掉多余的描述文本"""
    if not raw:
        return ""

    cleaned = re.sub(r'^[Tt]he (total |final )?(answer|result|force|expression) is\s+', '', raw)
    cleaned = re.sub(r'^[Aa]nswer:\s*', '', cleaned)
    cleaned = re.sub(r'\.\s*[A-Z][a-z]+.*$', '.', cleaned)
    cleaned = re.sub(r'\*\*', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    if '$$' in cleaned:
        matches = re.findall(r'\$\$(.*?)\$\$', cleaned, re.DOTALL)
        if matches:
            cleaned = f"$${matches[-1].strip()}$$"
    elif '$' in cleaned:
        matches = re.findall(r'\$(.*?)\$', cleaned)
        if matches:
            cleaned = f"${matches[-1].strip()}$"

    return cleaned


def infer_answer_type(answer: str, question: str) -> str:
    """推断答案类型"""
    if not answer:
        return ""

    answer_lower = answer.lower()
    question_lower = question.lower()

    if '=' in answer and not answer.startswith('$'):
        return "equation"

    if not any(c in answer for c in ['$', '=', '\\', '^', '_', '{', '}']):
        if any(word in question_lower for word in ['which', 'determine', 'identify', 'choose']):
            return "open-ended"

    if any(op in answer for op in ['<', '>', '≤', '≥', 'leq', 'geq', 'lt', 'gt']):
        return "inequality"

    return "expression"


def infer_answer_unit(answer: str, question: str, answer_type: str) -> str:
    """推断答案单位"""
    if not answer or answer_type == "open-ended":
        return ""

    if answer_type == "expression":
        return ""

    question_lower = question.lower()
    answer_lower = answer.lower()

    if re.search(r'\bd[a-z]+\s*/\s*dt\b', answer_lower) or re.search(r'\bd[a-z]+\s*/\s*dt\b', question_lower):
        return "m/s"

    unit_keywords = {
        'tension': 'N', 'force': 'N', 'lorentz force': 'N',
        'emf': 'V', 'electromotive force': 'V', 'voltage': 'V', 'potential': 'V',
        'power': 'W', 'energy': 'J', 'work': 'J',
        'radius': 'm', 'distance': 'm', 'length': 'm', 'orbit': 'm',
        'velocity': 'm/s', 'speed': 'm/s', 'acceleration': 'm/s²',
        'temperature': 'K', 'current': 'A', 'total charge': 'C',
        'charge transferred': 'C', 'charge': 'C',
        'magnetic field': 'T', 'resistance': 'Ω', 'capacitance': 'F',
        'inductance': 'H', 'frequency': 'Hz', 'time': 's',
        'mass': 'kg', 'momentum': 'kg·m/s', 'angular momentum': 'kg·m²/s',
        'decay rate': 'm/s', 'rate of change': 'm/s',
    }

    sorted_keywords = sorted(unit_keywords.items(), key=lambda x: len(x[0]), reverse=True)
    for keyword, unit in sorted_keywords:
        if keyword in question_lower:
            return unit

    if not any(c in answer for c in ['$', '=', '\\', '^', '_', '{', '}']):
        return ""

    return ""


def infer_modality(question_text: str, has_image: bool, image_paths: list[str] | None = None) -> str:
    """推断模态类型。
    
    四种模态类型：
    - text-only：问题完全使用文字描述，没有图表辅助
    - text+illustration figure：图表描述场景，文字提供描述
    - text+variable figure：图表明确关键变量或空间范围
    - text+data figure：图表呈现文本中未给出的数据、图表或函数
    
    如果问题含多个物理量求解，则返回列表包含多个类型。
    默认基于 has_image 做基础判断，LLM 可用时调用 LLM 精确分类。
    """
    if not has_image:
        return "text-only"
    
    # 尝试用 LLM 精确判断模态类型
    if os.environ.get("LLM_API_KEY") and question_text:
        try:
            from llm_client import infer_modality_with_llm
            result = infer_modality_with_llm(question_text, image_paths or [])
            if result and result in (
                "text-only", "text+illustration figure", "text+variable figure", "text+data figure"
            ):
                return result
        except Exception:
            pass
    
    # 启发式回退：有图默认 illustration figure
    return "text+illustration figure"


def extract_explicit_conditions(question: str) -> list[str]:
    """从问题文本提取显性条件"""
    conditions = []
    if not question:
        return conditions

    number_patterns = re.findall(r'\b\d+\.?\d*\s*(?:m|kg|s|N|V|A|T|Ω|Hz|K|J|W|C|F|H)\b', question)
    conditions.extend(number_patterns)

    assume_patterns = re.findall(r'[Aa]ssume\s+([^,.]+)', question)
    conditions.extend(assume_patterns)

    given_patterns = re.findall(r'[Gg]iven\s+([^,.]+)', question)
    conditions.extend(given_patterns)

    return conditions


def load_ipho_knowledge_points():
    """Load IPho taxonomy and return (points_set, l1_to_l2_to_l3s, all_l3_index)."""
    taxonomy_path = ROOT / "IPHO考纲_分类标签.json"
    if not taxonomy_path.exists():
        return set(), {}, {}

    taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    points: set[tuple[str, str, str]] = set()
    l1_to_l2_to_l3s: dict[str, dict[str, list[str]]] = {}
    all_l3_index: dict[str, tuple[str, str, str]] = {}

    for category in taxonomy.get("categories", []):
        l1 = category.get("level1", "")
        if l1 not in l1_to_l2_to_l3s:
            l1_to_l2_to_l3s[l1] = {}
        for subcategory in category.get("subcategories", []):
            l2 = subcategory.get("level2", "")
            l3s = subcategory.get("topics", [])
            l1_to_l2_to_l3s[l1][l2] = l3s
            for l3 in l3s:
                t = (l1, l2, l3)
                points.add(t)
                all_l3_index[l3.lower()] = t

    return points, l1_to_l2_to_l3s, all_l3_index


_KEYWORD_STOP_WORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
    "is", "are", "was", "were", "be", "been", "being", "as", "by", "with",
    "from", "its", "it", "no", "not", "can", "may", "has", "have", "that",
    "this", "these", "those", "also", "only", "both", "other", "some",
    "into", "over", "under", "between", "through", "due", "via", "per",
    "used", "using", "does", "need", "needed", "known",
}

_CONCEPT_TO_IPHO_L3: dict[str, tuple[str, str, str]] = {
    # ========== Electromagnetic fields ==========
    # Note: Order matters — earlier entries take precedence in the substring pass.
    # "torque on current-carrying conductor" / "torque on wire" → straight-wire
    # case → Ampère's force; explicit "current loop" / "dipole" stays Dipole moment.
    "torque on current carrying conductor": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "torque on current-carrying conductor": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "torque on current carrying wire": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "torque on current-carrying wire": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "torque on conductor": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "torque on wire": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "torque on current loop": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Dipole moment of a current loop"),
    "magnetic dipole torque": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Dipole moment of a current loop"),
    "electromagnetic torque": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Dipole moment of a current loop"),
    "magnetic torque": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Dipole moment of a current loop"),
    "magnetic dipole moment": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Dipole moment of a current loop"),
    "magnetic dipole energy": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Energy of a magnetic dipole in a magnetic field"),
    "lorentz force": ("Electromagnetic fields", "Basic concepts", "Lorentz force"),
    "magnetic force on wire": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "magnetic force wire": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "magnetic force on conductor": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "magnetic force conductor": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "force on current carrying": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "force on current-carrying": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "ampere force": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "ampère force": ("Electromagnetic fields", "Basic concepts", "Ampère's force"),
    "biot savart": ("Electromagnetic fields", "Basic concepts", "Biot-Savart law"),
    "biot-savart law": ("Electromagnetic fields", "Basic concepts", "Biot-Savart law"),
    "coulomb force": ("Electromagnetic fields", "Basic concepts", "Coulomb force"),
    "coulomb law": ("Electromagnetic fields", "Basic concepts", "Coulomb force"),
    "kirchhoff voltage": ("Electromagnetic fields", "Basic concepts", "Kirchhoff's voltage law"),
    "kirchhoff current": ("Electromagnetic fields", "Basic concepts", "Kirchhoff's current law"),
    "solenoid field": ("Electromagnetic fields", "Basic concepts", "B-field for simple symmetric systems like straight wire, circular loop and long solenoid"),
    "circular loop field": ("Electromagnetic fields", "Basic concepts", "B-field on the axis of a circular current loop"),
    "emf induction": ("Electromagnetic fields", "Integral forms of Maxwell's equations", "Faraday's law"),
    "motional emf": ("Electromagnetic fields", "Integral forms of Maxwell's equations", "Faraday's law"),
    "faraday law": ("Electromagnetic fields", "Integral forms of Maxwell's equations", "Faraday's law"),
    "electromagnetic induction": ("Electromagnetic fields", "Integral forms of Maxwell's equations", "Faraday's law"),
    "gauss law": ("Electromagnetic fields", "Integral forms of Maxwell's equations", "Gauss' law (for E- and B-fields)"),
    "ampere law": ("Electromagnetic fields", "Integral forms of Maxwell's equations", "Ampère's law (with Maxwell correction)"),
    "image charge": ("Electromagnetic fields", "Integral forms of Maxwell's equations", "Method of image charges"),
    "image charges": ("Electromagnetic fields", "Integral forms of Maxwell's equations", "Method of image charges"),
    "maxwell equations": ("Electromagnetic fields", "Integral forms of Maxwell's equations", "Gauss' law (for E- and B-fields)"),
    "lenz law": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Lenz's law"),
    "eddy current": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Eddy currents"),
    "cyclotron frequency": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Cyclotron frequency"),
    "helicoidal motion": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Charges in magnetic field: helicoidal motion"),
    "field energy density": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Energy density of electric and magnetic fields"),
    "permittivity": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Relative permittivity and permeability of electric and magnetic materials"),
    "permeability": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Relative permittivity and permeability of electric and magnetic materials"),
    "ohm law": ("Electromagnetic fields", "Circuits", "Linear resistors and Ohm's law"),
    "ohms law": ("Electromagnetic fields", "Circuits", "Linear resistors and Ohm's law"),
    "ohmic heating": ("Electromagnetic fields", "Circuits", "Joule's law"),
    "joule heating": ("Electromagnetic fields", "Circuits", "Joule's law"),
    "joule law": ("Electromagnetic fields", "Circuits", "Joule's law"),
    "capacitance": ("Electromagnetic fields", "Circuits", "Capacitors and capacitance (also for a single electrode with respect to infinity)"),
    "inductance": ("Electromagnetic fields", "Circuits", "Self-induction and inductance"),
    "rc circuit": ("Electromagnetic fields", "Circuits", "Time constants for RL and RC circuits"),
    "rl circuit": ("Electromagnetic fields", "Circuits", "Time constants for RL and RC circuits"),
    "ac impedance": ("Electromagnetic fields", "Circuits", "Impedance of resistors, inductors, capacitors, and combination circuits"),
    "resonance circuit": ("Electromagnetic fields", "Circuits", "Current and voltage resonance"),
    # ========== Mechanics ==========
    "circular orbit energy": ("Mechanics", "Celestial mechanics", "Energy of a point mass on an elliptical orbit"),
    "elliptical orbit": ("Mechanics", "Celestial mechanics", "Energy of a point mass on an elliptical orbit"),
    "orbital decay": ("Mechanics", "Dynamics", "Mechanical work and power"),
    "gravitational potential energy": ("Mechanics", "Celestial mechanics", "Gravitational potential"),
    "gravitational potential": ("Mechanics", "Celestial mechanics", "Gravitational potential"),
    "law of gravity": ("Mechanics", "Celestial mechanics", "Law of gravity"),
    "newton gravity": ("Mechanics", "Celestial mechanics", "Law of gravity"),
    "kepler orbit": ("Mechanics", "Celestial mechanics", "Kepler's laws (no derivation needed for first and third law)"),
    "kepler law": ("Mechanics", "Celestial mechanics", "Kepler's laws (no derivation needed for first and third law)"),
    "newton second law": ("Mechanics", "Dynamics", "Newton's second law (in vector form and via projections (components))"),
    "newtons second law": ("Mechanics", "Dynamics", "Newton's second law (in vector form and via projections (components))"),
    "kinetic energy": ("Mechanics", "Dynamics", "Kinetic energy for translational and rotational motions"),
    "rotational kinetic energy": ("Mechanics", "Dynamics", "Kinetic energy for translational and rotational motions"),
    "potential energy": ("Mechanics", "Dynamics", "Potential energy for simple force fields (also as a line integral of the force field)"),
    "angular momentum": ("Mechanics", "Dynamics", "Angular momentum"),
    "linear momentum": ("Mechanics", "Dynamics", "Momentum"),
    "conservation of momentum": ("Mechanics", "Dynamics", "Their conservation laws"),
    "conservation of energy": ("Mechanics", "Dynamics", "Their conservation laws"),
    "mechanical work": ("Mechanics", "Dynamics", "Mechanical work and power"),
    "moment of inertia": ("Mechanics", "Dynamics", "Moment of inertia for simple bodies (ring, disk, sphere, hollow sphere, rod)"),
    "parallel axis theorem": ("Mechanics", "Dynamics", "Parallel axis theorem"),
    "centrifugal force": ("Mechanics", "Dynamics", "Centrifugal force"),
    "inertial frame": ("Mechanics", "Dynamics", "Inertial and non-inertial frames of reference"),
    "non inertial frame": ("Mechanics", "Dynamics", "Inertial and non-inertial frames of reference"),
    "friction force": ("Mechanics", "Statics", "Static and kinetic friction force"),
    "kinetic friction": ("Mechanics", "Statics", "Static and kinetic friction force"),
    "static friction": ("Mechanics", "Statics", "Static and kinetic friction force"),
    "hooke law": ("Mechanics", "Statics", "Hooke's law"),
    "hookes law": ("Mechanics", "Statics", "Hooke's law"),
    "spring constant": ("Mechanics", "Statics", "Hooke's law"),
    "young modulus": ("Mechanics", "Statics", "Young modulus"),
    "stress strain": ("Mechanics", "Statics", "Stress"),
    "torque balance": ("Mechanics", "Statics", "Torque balance (only for one- and two-dimensional geometry)"),
    "force balance": ("Mechanics", "Statics", "Equilibrium conditions: force balance (vectorially or in terms of projections)"),
    "center of mass": ("Mechanics", "Statics", "Finding the centre of mass of a system via summation or via integration"),
    "centre of mass": ("Mechanics", "Statics", "Finding the centre of mass of a system via summation or via integration"),
    "tension force": ("Mechanics", "Statics", "Tension force"),
    "centripetal acceleration": ("Mechanics", "Kinematics", "Centripetal and tangential acceleration"),
    "tangential acceleration": ("Mechanics", "Kinematics", "Centripetal and tangential acceleration"),
    "angular velocity": ("Mechanics", "Kinematics", "Addition of velocities and angular velocities"),
    "rigid body rotation": ("Mechanics", "Kinematics", "Motion of a rigid body as a rotation around an instantaneous centre of rotation"),
    "buoyancy": ("Mechanics", "Hydrodynamics", "Buoyancy"),
    "bernoulli equation": ("Mechanics", "Hydrodynamics", "Bernoulli equation"),
    "continuity equation": ("Mechanics", "Hydrodynamics", "Continuity law"),
    "surface tension": ("Mechanics", "Hydrodynamics", "Surface tension and the associated energy"),
    "capillary pressure": ("Mechanics", "Hydrodynamics", "Capillary pressure"),
    "fluid pressure": ("Mechanics", "Hydrodynamics", "Pressure"),
    # ========== Oscillations and waves ==========
    "harmonic oscillation": ("Oscillations and waves", "Single oscillator", "Harmonic oscillations: equation of motion"),
    "simple harmonic motion": ("Oscillations and waves", "Single oscillator", "Harmonic oscillations: equation of motion"),
    "harmonic oscillator": ("Oscillations and waves", "Single oscillator", "Harmonic oscillations: equation of motion"),
    "angular frequency": ("Oscillations and waves", "Single oscillator", "Angular frequency"),
    "natural frequency": ("Oscillations and waves", "Single oscillator", "Frequency"),
    "physical pendulum": ("Oscillations and waves", "Single oscillator", "Physical pendulum and its reduced length"),
    "damped oscillation": ("Oscillations and waves", "Single oscillator", "Exponential decay of damped oscillations"),
    "damping": ("Oscillations and waves", "Single oscillator", "Exponential decay of damped oscillations"),
    "forced oscillation": ("Oscillations and waves", "Single oscillator", "Resonance of sinusoidally forced oscillators: amplitude of steady state oscillations"),
    "driven oscillation": ("Oscillations and waves", "Single oscillator", "Resonance of sinusoidally forced oscillators: amplitude of steady state oscillations"),
    "mechanical resonance": ("Oscillations and waves", "Single oscillator", "Resonance of sinusoidally forced oscillators: amplitude of steady state oscillations"),
    "phase shift": ("Oscillations and waves", "Single oscillator", "Phase shift of steady state oscillations"),
    "lc oscillation": ("Oscillations and waves", "Single oscillator", "Free oscillations of LC circuits"),
    "doppler effect": ("Oscillations and waves", "Waves", "The classical Doppler effect"),
    "classical doppler": ("Oscillations and waves", "Waves", "The classical Doppler effect"),
    # de Broglie / matter wave entries here (BEFORE generic "wavelength")
    # so they win the substring pass for "de Broglie wavelength" inputs.
    "de broglie wavelength": ("Quantum Physics", "Probability waves", "Particles as waves: relationship between the frequency and energy"),
    "matter wavelength": ("Quantum Physics", "Probability waves", "Particles as waves: relationship between the frequency and energy"),
    "wavelength": ("Oscillations and waves", "Waves", "Wavelength"),
    "wave vector": ("Oscillations and waves", "Waves", "Wave vector"),
    "phase velocity": ("Oscillations and waves", "Waves", "Phase and group velocities"),
    "group velocity": ("Oscillations and waves", "Waves", "Phase and group velocities"),
    "snell law": ("Oscillations and waves", "Waves", "Snell's law"),
    "snells law": ("Oscillations and waves", "Waves", "Snell's law"),
    "fermat principle": ("Oscillations and waves", "Waves", "Waves in inhomogeneous media: Fermat's principle"),
    "transverse wave": ("Oscillations and waves", "Waves", "Transverse and longitudinal waves"),
    "longitudinal wave": ("Oscillations and waves", "Waves", "Transverse and longitudinal waves"),
    "sound wave": ("Oscillations and waves", "Waves", "Sound waves: speed as a function of pressure (Young's or bulk modulus) and density"),
    "mach cone": ("Oscillations and waves", "Waves", "Mach cone"),
    "wave energy": ("Oscillations and waves", "Waves", "Energy carried by waves: proportionality to the square of the amplitude"),
    "standing wave": ("Oscillations and waves", "Interference and diffraction", "Standing waves"),
    "beats": ("Oscillations and waves", "Interference and diffraction", "Beats"),
    "thin film interference": ("Oscillations and waves", "Interference and diffraction", "Interference due to thin films"),
    "double slit": ("Oscillations and waves", "Interference and diffraction", "Diffraction from one and two slits"),
    "single slit": ("Oscillations and waves", "Interference and diffraction", "Diffraction from one and two slits"),
    "diffraction grating": ("Oscillations and waves", "Interference and diffraction", "Diffraction grating"),
    "huygens principle": ("Oscillations and waves", "Interference and diffraction", "Huygens' principle"),
    "bragg reflection": ("Oscillations and waves", "Interference and diffraction", "Bragg reflection"),
    "wave coherence": ("Oscillations and waves", "Interference and diffraction", "Superposition of waves: coherence"),
    "refractive index": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Refractive index"),
    "index of refraction": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Refractive index"),
    "brewster angle": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Brewster angle"),
    "linear polarization": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Linear polarisation"),
    "linear polarisation": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Linear polarisation"),
    "malus law": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Malus' law"),
    "malus": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Malus' law"),
    "polarizer": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Polarisers"),
    "thin lens": ("Oscillations and waves", "Geometrical optics and photometry", "Thin lens approximation"),
    "thin lens equation": ("Oscillations and waves", "Geometrical optics and photometry", "Thin lens equation"),
    "lens equation": ("Oscillations and waves", "Geometrical optics and photometry", "Thin lens equation"),
    "geometrical optics": ("Oscillations and waves", "Geometrical optics and photometry", "Approximation of geometrical optics: rays and optical images"),
    "ray optics": ("Oscillations and waves", "Geometrical optics and photometry", "Approximation of geometrical optics: rays and optical images"),
    "optical image": ("Oscillations and waves", "Geometrical optics and photometry", "Construction of images created by ideal thin lenses"),
    "luminous flux": ("Oscillations and waves", "Geometrical optics and photometry", "Luminous flux and its continuity"),
    "telescope": ("Oscillations and waves", "Optical devices", "Telescopes and microscopes: magnification and resolving power"),
    "microscope": ("Oscillations and waves", "Optical devices", "Telescopes and microscopes: magnification and resolving power"),
    "interferometer": ("Oscillations and waves", "Optical devices", "Interferometers"),
    # ========== Relativity ==========
    "principle of relativity": ("Relativity", "Relativity", "Principle of relativity"),
    "lorentz transformation": ("Relativity", "Relativity", "Lorentz transformations for the time and spatial coordinate"),
    "time dilation": ("Relativity", "Relativity", "Time dilation"),
    "length contraction": ("Relativity", "Relativity", "Length contraction"),
    "mass energy equivalence": ("Relativity", "Relativity", "Mass-energy equivalence"),
    "rest energy": ("Relativity", "Relativity", "Mass-energy equivalence"),
    "e mc2": ("Relativity", "Relativity", "Mass-energy equivalence"),
    "e equals mc squared": ("Relativity", "Relativity", "Mass-energy equivalence"),
    "rest mass": ("Relativity", "Relativity", "Invariance of the spacetime interval and of the rest mass"),
    "spacetime interval": ("Relativity", "Relativity", "Invariance of the spacetime interval and of the rest mass"),
    "relativistic doppler": ("Relativity", "Relativity", "Relativistic Doppler effect"),
    "photon energy": ("Relativity", "Relativity", "Energy and momentum of photons"),
    "photon momentum": ("Relativity", "Relativity", "Energy and momentum of photons"),
    "relativistic momentum": ("Relativity", "Relativity", "Lorentz transformations for the energy and momentum"),
    "relativistic energy": ("Relativity", "Relativity", "Lorentz transformations for the energy and momentum"),
    "velocity addition": ("Relativity", "Relativity", "Addition of parallel velocities"),
    "simultaneity": ("Relativity", "Relativity", "Relativity of simultaneity"),
    "relativistic equation of motion": ("Relativity", "Relativity", "Relativistic equation of motion"),
    # ========== Quantum Physics ==========
    "de broglie": ("Quantum Physics", "Probability waves", "Particles as waves: relationship between the frequency and energy"),
    "matter wave": ("Quantum Physics", "Probability waves", "Particles as waves: relationship between the frequency and energy"),
    "wave particle duality": ("Quantum Physics", "Probability waves", "Particles as waves: relationship between the frequency and energy"),
    "energy levels hydrogen": ("Quantum Physics", "Probability waves", "Energy levels of hydrogen-like atoms (circular orbits only)"),
    "hydrogen atom": ("Quantum Physics", "Probability waves", "Energy levels of hydrogen-like atoms (circular orbits only)"),
    "bohr model": ("Quantum Physics", "Probability waves", "Energy levels of hydrogen-like atoms (circular orbits only)"),
    "parabolic potential": ("Quantum Physics", "Probability waves", "Energy levels of parabolic potentials"),
    "quantum harmonic oscillator": ("Quantum Physics", "Probability waves", "Energy levels of parabolic potentials"),
    "angular momentum quantization": ("Quantum Physics", "Probability waves", "Quantization of angular momentum"),
    "uncertainty principle": ("Quantum Physics", "Probability waves", "Uncertainty principle for the coordinate and momentum"),
    "heisenberg uncertainty": ("Quantum Physics", "Probability waves", "Uncertainty principle for the coordinate and momentum"),
    "energy time uncertainty": ("Quantum Physics", "Probability waves", "Uncertainty principle for the conjugate pairs of time and energy"),
    "photoelectric effect": ("Quantum Physics", "Structure of matter", "Photoelectric effect"),
    "compton scattering": ("Quantum Physics", "Structure of matter", "Compton scattering"),
    "emission spectra": ("Quantum Physics", "Structure of matter", "Emission and absorption spectra for hydrogen-like atoms"),
    "absorption spectra": ("Quantum Physics", "Structure of matter", "Emission and absorption spectra for hydrogen-like atoms"),
    "atomic spectra": ("Quantum Physics", "Structure of matter", "Emission and absorption spectra for hydrogen-like atoms"),
    "spectral linewidth": ("Quantum Physics", "Structure of matter", "Spectral width"),
    "linewidth": ("Quantum Physics", "Structure of matter", "Spectral width"),
    "lifetime excited state": ("Quantum Physics", "Structure of matter", "Lifetime of excited states"),
    "pauli exclusion": ("Quantum Physics", "Structure of matter", "Pauli exclusion principle for Fermi particles"),
    "alpha decay": ("Quantum Physics", "Structure of matter", "Alpha-, beta- and gamma-decays"),
    "beta decay": ("Quantum Physics", "Structure of matter", "Alpha-, beta- and gamma-decays"),
    "gamma decay": ("Quantum Physics", "Structure of matter", "Alpha-, beta- and gamma-decays"),
    "nuclear fission": ("Quantum Physics", "Structure of matter", "Fission"),
    "nuclear fusion": ("Quantum Physics", "Structure of matter", "Fusion"),
    "mass defect": ("Quantum Physics", "Structure of matter", "Mass defect"),
    "half life": ("Quantum Physics", "Structure of matter", "Half life"),
    "half-life": ("Quantum Physics", "Structure of matter", "Half life"),
    "radioactive decay": ("Quantum Physics", "Structure of matter", "Exponential decay"),
    "neutron capture": ("Quantum Physics", "Structure of matter", "Neutron capture"),
    "atomic nuclei": ("Quantum Physics", "Structure of matter", "Atomic nuclei"),
    "molecular spectra": ("Quantum Physics", "Structure of matter", "Spectra for molecules due to molecular oscillations"),
    # ========== Thermodynamics and statistical physics ==========
    "planck blackbody": ("Thermodynamics and statistical physics", "Statistical physics", "Planck's law (explained qualitatively, does not need to be remembered)"),
    "planck law": ("Thermodynamics and statistical physics", "Statistical physics", "Planck's law (explained qualitatively, does not need to be remembered)"),
    "planck radiation": ("Thermodynamics and statistical physics", "Statistical physics", "Planck's law (explained qualitatively, does not need to be remembered)"),
    "wien displacement": ("Thermodynamics and statistical physics", "Statistical physics", "Wien's displacement law"),
    "radiative cooling": ("Thermodynamics and statistical physics", "Statistical physics", "Stefan-Boltzmann law"),
    "stefan boltzmann": ("Thermodynamics and statistical physics", "Statistical physics", "Stefan-Boltzmann law"),
    "blackbody radiation": ("Thermodynamics and statistical physics", "Statistical physics", "Stefan-Boltzmann law"),
    "black body radiation": ("Thermodynamics and statistical physics", "Statistical physics", "Stefan-Boltzmann law"),
    "thermal equilibrium": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Concepts of thermal equilibrium and reversible processes"),
    "first law thermodynamics": ("Thermodynamics and statistical physics", "Classical thermodynamics", "First and second laws of thermodynamics"),
    "second law thermodynamics": ("Thermodynamics and statistical physics", "Classical thermodynamics", "First and second laws of thermodynamics"),
    "entropy": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Entropy"),
    "internal energy": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Internal energy"),
    "ideal gas law": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Ideal gas law"),
    "ideal gas": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Ideal gas law"),
    "boltzmann factor": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Boltzmann factor"),
    "equipartition": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Equipartition theorem"),
    "specific heat": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Specific heat for isobaric and isochoric processes"),
    "carnot cycle": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Forward and reverse Carnot cycle on ideal gas and its efficiency"),
    "heat engine efficiency": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Efficiency of non-ideal heat engines"),
    "adiabatic process": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Isothermal, isobaric, isochoric, and adiabatic processes"),
    "isothermal process": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Isothermal, isobaric, isochoric, and adiabatic processes"),
    "rms speed": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Root-mean-square speed of molecules"),
    "root mean square speed": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Root-mean-square speed of molecules"),
    "avogadro number": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Kinetic theory of ideal gases: Avogadro number"),
    "kinetic theory": ("Thermodynamics and statistical physics", "Classical thermodynamics", "Kinetic theory of ideal gases: Avogadro number"),
    "phase transition": ("Thermodynamics and statistical physics", "Heat transfer and phase transitions", "Phase transitions (boiling, evaporation, melting, sublimation)"),
    "latent heat": ("Thermodynamics and statistical physics", "Heat transfer and phase transitions", "Latent heat"),
    "saturated vapor pressure": ("Thermodynamics and statistical physics", "Heat transfer and phase transitions", "Saturated vapour pressure"),
    "saturated vapour pressure": ("Thermodynamics and statistical physics", "Heat transfer and phase transitions", "Saturated vapour pressure"),
    "heat conductivity": ("Thermodynamics and statistical physics", "Heat transfer and phase transitions", "Concept of heat conductivity"),
    "heat conduction": ("Thermodynamics and statistical physics", "Heat transfer and phase transitions", "Concept of heat conductivity"),
    "dalton law": ("Thermodynamics and statistical physics", "Heat transfer and phase transitions", "Dalton's law"),
    # Common phrasings that don't appear verbatim in the syllabus
    "photoelectric": ("Quantum Physics", "Structure of matter", "Photoelectric effect"),
    "photoelectric ionization": ("Quantum Physics", "Structure of matter", "Photoelectric effect"),
    "photoelectric ionisation": ("Quantum Physics", "Structure of matter", "Photoelectric effect"),
    "photoelectric emission": ("Quantum Physics", "Structure of matter", "Photoelectric effect"),
    "photoeffect": ("Quantum Physics", "Structure of matter", "Photoelectric effect"),
    "torque about fixed point": ("Mechanics", "Statics", "Torque balance (only for one- and two-dimensional geometry)"),
    "torque balance": ("Mechanics", "Statics", "Torque balance (only for one- and two-dimensional geometry)"),
    "rolling motion": ("Mechanics", "Kinematics", "Motion of a rigid body as a rotation around an instantaneous centre of rotation"),
    "rolling without slipping": ("Mechanics", "Kinematics", "Motion of a rigid body as a rotation around an instantaneous centre of rotation"),
    "instantaneous axis of rotation": ("Mechanics", "Kinematics", "Motion of a rigid body as a rotation around an instantaneous centre of rotation"),
    "rigid body rotation": ("Mechanics", "Kinematics", "Motion of a rigid body as a rotation around an instantaneous centre of rotation"),
    "polarization of light": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Linear polarisation"),
    "polarisation of light": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Linear polarisation"),
    "light polarization": ("Oscillations and waves", "Interaction of electromagnetic waves with matter", "Linear polarisation"),
    "ferromagnetic hysteresis": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Hysteresis and dissipation"),
    "magnetic hysteresis": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Hysteresis and dissipation"),
    "hysteresis loop": ("Electromagnetic fields", "Interaction of matter with electric and magnetic fields", "Hysteresis and dissipation"),
}


# Merge auto-generated aliases (from build_concept_aliases.py) covering ALL
# 227 IPhO Level-3 leaves. Manual entries above take precedence (handled by
# dict-update order: auto entries are added only if the key is not present).
try:
    from _concept_aliases_auto import CONCEPT_ALIASES_AUTO  # type: ignore
    for _auto_key, _auto_val in CONCEPT_ALIASES_AUTO.items():
        if _auto_key not in _CONCEPT_TO_IPHO_L3:
            _CONCEPT_TO_IPHO_L3[_auto_key] = _auto_val
except ImportError:
    pass


# Domain router: lightweight Level-1 classifier from raw English text.
# Used to (1) inject domain-scoped examples in the LLM prompt, (2) constrain
# fuzzy matching when the LLM-emitted L1 conflicts with the textual context.
_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Mechanics": (
        "torque", "kinetic energy", "potential energy", "orbit", "kepler",
        "moment of inertia", "newton", "friction", "spring", "pendulum",
        "rigid body", "angular momentum", "linear momentum", "collision",
        "buoyancy", "bernoulli", "fluid", "pressure", "centripetal", "gravity",
    ),
    "Electromagnetic fields": (
        "current", "voltage", "lorentz", "ampere", "ampère", "emf", "ohm",
        "joule heating", "capacit", "induct", "kirchhoff", "magnetic field",
        "electric field", "biot", "savart", "gauss", "faraday", "lenz",
        "maxwell", "dipole", "permittivity", "permeability", "circuit",
    ),
    "Thermodynamics and statistical physics": (
        "entropy", "carnot", "ideal gas", "adiabatic", "isothermal",
        "thermal", "heat capacity", "latent heat", "boltzmann",
        "stefan", "planck", "blackbody", "black body", "vapor", "vapour",
        "phase transition", "evaporation", "boiling", "fermi", "bose",
    ),
    "Oscillations and waves": (
        "wavelength", "diffraction", "interference", "doppler", "snell",
        "refraction", "polariz", "polaris", "malus", "lens", "mirror",
        "thin film", "bragg", "huygens", "standing wave", "beat", "resonance",
        "harmonic oscillator", "lc circuit", "telescope", "microscope",
        "fermat", "phase velocity", "group velocity",
    ),
    "Quantum Physics": (
        "photon", "photoelectric", "compton", "de broglie", "matter wave",
        "uncertainty principle", "heisenberg", "bohr", "hydrogen atom",
        "energy level", "spectral", "linewidth", "alpha decay", "beta decay",
        "gamma decay", "fission", "fusion", "half life", "radioactive",
        "neutron", "atomic nucle", "pauli",
    ),
    "Relativity": (
        "lorentz transform", "time dilation", "length contraction",
        "rest mass", "spacetime", "relativistic", "simultaneity",
        "mass-energy", "mass energy equivalence", "rest frame",
    ),
}


def detect_domains(text: str) -> list[str]:
    """Return Level-1 domains whose keywords occur in the text (lowercase)."""
    if not text:
        return []
    low = text.lower()
    hits: list[str] = []
    for domain, kws in _DOMAIN_KEYWORDS.items():
        if any(kw in low for kw in kws):
            hits.append(domain)
    return hits


def _extract_keywords(text: str) -> set[str]:
    words = re.findall(r'[a-z]{3,}', text.lower())
    return {w for w in words if w not in _KEYWORD_STOP_WORDS}


def _keyword_jaccard(set_a: set[str], set_b: set[str]) -> float:
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


def _keyword_match_l3(l3_text: str, all_l3_index: dict[str, tuple[str, str, str]]) -> tuple[str, str, str] | None:
    lowered = l3_text.lower().strip()
    # Replace hyphens/punctuation with spaces so that variants like
    # "mass-energy equivalence" tokenize identically to "mass energy equivalence".
    # This is critical for substring matching against alias keys (which are
    # space-separated words) and prevents short generic keys (e.g. "energy")
    # from spuriously matching compound terms (e.g. "mass-energy equivalence").
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    normalized = " ".join(w for w in lowered.split() if w not in _KEYWORD_STOP_WORDS)
    input_word_set = set(re.findall(r'[a-z]+', normalized))

    # Step 5a: concept mapping lookup
    # First pass: substring match on normalized form (ordered phrases)
    for concept_key, canonical in _CONCEPT_TO_IPHO_L3.items():
        norm_key = " ".join(w for w in concept_key.split() if w not in _KEYWORD_STOP_WORDS)
        if norm_key in normalized:
            return canonical
    # Second pass: word-subset match (handles permutations like
    # "Magnetic force and torque on wire" -> {magnetic, force, wire})
    best_concept = None
    best_concept_len = 0
    for concept_key, canonical in _CONCEPT_TO_IPHO_L3.items():
        key_words = {w for w in re.findall(r'[a-z]+', concept_key.lower())
                     if w not in _KEYWORD_STOP_WORDS}
        if len(key_words) >= 2 and key_words.issubset(input_word_set):
            if len(key_words) > best_concept_len:
                best_concept_len = len(key_words)
                best_concept = canonical
    if best_concept is not None:
        return best_concept

    # Step 5b: Jaccard keyword overlap
    input_kw = _extract_keywords(l3_text)
    if len(input_kw) < 2:
        return None

    best_match = None
    best_score = 0.0

    for l3_lower, canonical in all_l3_index.items():
        l3_kw = _extract_keywords(l3_lower)
        if len(l3_kw) < 2:
            continue
        score = _keyword_jaccard(input_kw, l3_kw)
        if score > best_score:
            best_score = score
            best_match = canonical

    intersection = input_kw & _extract_keywords(best_match[2].lower()) if best_match else set()
    if best_match and best_score >= 0.2 and len(intersection) >= 2:
        return best_match
    return None


_UNMATCHED_LOG: list[dict] = []


def get_unmatched_log() -> list[dict]:
    """Return the in-memory unmatched-knowledge-point log (for diagnostic dumps)."""
    return list(_UNMATCHED_LOG)


def reset_unmatched_log() -> None:
    """Clear the unmatched log (call between batch runs to avoid stale entries)."""
    _UNMATCHED_LOG.clear()


def normalize_related_knowledge_points(
    raw_points: list,
    ipho_points: set[tuple[str, str, str]],
    l1_to_l2_to_l3s: dict[str, dict[str, list[str]]],
    all_l3_index: dict[str, tuple[str, str, str]],
    *,
    context_id: str = "",
) -> list[list[str]]:
    if not raw_points:
        return []
    if not ipho_points:
        return [[str(k[0]), str(k[1]), str(k[2])] for k in raw_points if isinstance(k, list) and len(k) >= 3]

    normalized: list[list[str]] = []
    seen: set[tuple[str, str, str]] = set()

    for kp in raw_points:
        if not isinstance(kp, list) or len(kp) < 3:
            continue
        try_l1, try_l2, try_l3 = str(kp[0]), str(kp[1]), str(kp[2])

        # Step 1: exact match on all three
        candidate = (try_l1, try_l2, try_l3)
        if candidate in ipho_points:
            if candidate not in seen:
                normalized.append([try_l1, try_l2, try_l3])
                seen.add(candidate)
            continue

        match = None

        # Step 1.5: concept mapping lookup (before fuzzy matching to avoid false positives)
        concept_match = _keyword_match_l3(try_l3, all_l3_index)
        if concept_match and concept_match not in seen:
            normalized.append([concept_match[0], concept_match[1], concept_match[2]])
            seen.add(concept_match)
            continue

        # Step 2: L1 exact match → fuzzy L2 → best L3 within that L2
        if try_l1 in l1_to_l2_to_l3s:
            valid_l2s = list(l1_to_l2_to_l3s[try_l1].keys())
            close_l2 = get_close_matches(try_l2.lower(), [l.lower() for l in valid_l2s], n=1, cutoff=0.6)
            if close_l2:
                matched_l2 = valid_l2s[[l.lower() for l in valid_l2s].index(close_l2[0])]
                l3s = l1_to_l2_to_l3s[try_l1][matched_l2]
                close_l3 = get_close_matches(try_l3.lower(), [l.lower() for l in l3s], n=1, cutoff=0.5)
                if close_l3:
                    matched_l3 = l3s[[l.lower() for l in l3s].index(close_l3[0])]
                    match = (try_l1, matched_l2, matched_l3)

        # Step 3: fuzzy L1 → fuzzy L2 → best L3
        if match is None:
            all_l1s = list(l1_to_l2_to_l3s.keys())
            close_l1 = get_close_matches(try_l1.lower(), [l.lower() for l in all_l1s], n=1, cutoff=0.7)
            if close_l1:
                matched_l1 = all_l1s[[l.lower() for l in all_l1s].index(close_l1[0])]
                valid_l2s = list(l1_to_l2_to_l3s[matched_l1].keys())
                close_l2 = get_close_matches(try_l2.lower(), [l.lower() for l in valid_l2s], n=1, cutoff=0.6)
                if close_l2:
                    matched_l2 = valid_l2s[[l.lower() for l in valid_l2s].index(close_l2[0])]
                    l3s = l1_to_l2_to_l3s[matched_l1][matched_l2]
                    close_l3 = get_close_matches(try_l3.lower(), [l.lower() for l in l3s], n=1, cutoff=0.5)
                    if close_l3:
                        matched_l3 = l3s[[l.lower() for l in l3s].index(close_l3[0])]
                        match = (matched_l1, matched_l2, matched_l3)

        # Step 4: global fuzzy match on L3 only
        if match is None:
            close_l3 = get_close_matches(try_l3.lower(), list(all_l3_index.keys()), n=1, cutoff=0.65)
            if close_l3:
                match = all_l3_index[close_l3[0]]

        if match and match not in seen:
            normalized.append([match[0], match[1], match[2]])
            seen.add(match)
        elif match is None:
            # 4-step fuzzy + concept lookup all failed: log for offline review.
            _UNMATCHED_LOG.append({
                "context_id": context_id,
                "raw": [try_l1, try_l2, try_l3],
            })

    return normalized


def try_llm_fill(
    sub_id: str,
    question_text: str,
    solution_text: str,
    grading_text: str,
    background_text: str,
) -> dict:
    """尝试通过 LLM 填充标注字段。若 LLM 不可用或失败则返回空 dict。"""
    if not os.environ.get("LLM_API_KEY"):
        return {}

    try:
        from llm_client import annotate_subquestion

        print(f"[fill] calling LLM for {sub_id} ...")
        result = annotate_subquestion(
            sub_id=sub_id,
            question_text=question_text,
            solution_text=solution_text,
            grading_rubric_text=grading_text,
            background_text=background_text,
        )
        print(f"[fill] LLM returned for {sub_id}: difficulty={result.get('difficulty', '?')}")
        return result
    except ImportError as e:
        print(f"[fill] LLM not available (import error): {e}")
        return {}
    except Exception as e:
        print(f"[fill] LLM call failed for {sub_id}: {e}")
        return {}


def build_annotation_new(sub_pack: dict, meta: dict) -> tuple[list[dict], list[str]]:
    """生成新格式的标注 JSON（完全对齐最终模板.json）"""
    annotations = []
    todos: list[str] = []
    seen_parts: set[str] = set()
    ipho_points, l1_to_l2_to_l3s, all_l3_index = load_ipho_knowledge_points()

    grading_text = sub_pack.get("grading_rubric_text", "")

    for s in sub_pack["subquestions"]:
        sub_id = s["id"]
        parts = sub_id.split(".")
        part_letter = parts[0] if len(parts) > 0 else ""

        question_text = s.get("original_question", "")
        solution_text = s.get("solution_process", "")
        raw_background_info = s.get("background_info", "")
        # 背景信息去重：同一 Part 只保留第一个子题目的背景信息
        if part_letter and part_letter in seen_parts:
            background_info = ""
        else:
            background_info = raw_background_info
            if part_letter:
                seen_parts.add(part_letter)
        context = s.get("context", "")
        rubric = s.get("rubric_items", [])

        final_raw = s.get("final_answer_raw", "").strip()
        final_cleaned = clean_final_answer(final_raw)
        final_arr = [final_cleaned] if final_cleaned else [""]

        answer_type = infer_answer_type(final_cleaned, question_text)
        answer_unit = infer_answer_unit(final_cleaned, question_text, answer_type)
        # 初始模态类型（无图片时 text-only，后续 sync_image_refs 会更新）
        modality = infer_modality(question_text, has_image=False)
        explicit_conditions = extract_explicit_conditions(question_text)

        llm_result = try_llm_fill(
            sub_id=sub_id,
            question_text=question_text,
            solution_text=solution_text,
            grading_text=grading_text,
            background_text=background_info,
        )

        llm_kp = llm_result.get("related_knowledge_points", [])
        llm_model = llm_result.get("physical_model", "")
        llm_scenario = llm_result.get("physical_scenario", "")
        llm_explicit = llm_result.get("explicit_conditions", [])
        llm_implicit = llm_result.get("implicit_conditions", [])
        llm_rubric = llm_result.get("grading_rubric", [])
        llm_core = llm_result.get("core_idea", "")
        llm_difficulty = llm_result.get("difficulty", "")
        llm_answer_type = llm_result.get("answer_type", "")

        related_kp = normalize_related_knowledge_points(
            llm_kp, ipho_points, l1_to_l2_to_l3s, all_l3_index,
            context_id=str(sub_id),
        )

        if llm_explicit:
            explicit_conditions = [str(c) for c in llm_explicit]

        implicit_conditions = []
        for ic in llm_implicit:
            if isinstance(ic, dict):
                implicit_conditions.append({
                    "条件原文": str(ic.get("condition_text", ic.get("条件原文", ""))),
                    "隐藏条件": str(ic.get("hidden_meaning", ic.get("隐藏条件", ""))),
                })

        difficulty = llm_difficulty if llm_difficulty in ("easy", "medium", "hard") else ""
        final_answer_type_val = llm_answer_type if llm_answer_type in (
            "expression", "numerical", "choice", "equation", "open-ended", "inequality"
        ) else answer_type

        if llm_rubric:
            rubric = [_normalize_latex(str(r)) for r in llm_rubric]
        elif rubric:
            rubric = [_normalize_latex(r) for r in rubric]
        else:
            rubric = []
        core_idea = llm_core or ""
        physical_model = llm_model or ""
        physical_scenario = llm_scenario or ""

        # 只针对版块3，去掉A.1/B.1等的点
        block3 = sub_id.replace(".", "") if re.match(r"^[A-Z]\.\d+$", sub_id) else sub_id
        annotation = {
            "标注基础信息": {
                "来源": meta.get("source", ""),
                "补充": meta.get("supplement", ""),
                "年份": meta.get("year", ""),
                "版块1": meta.get("section_1", ""),
                "版块2": f"Part-{part_letter}" if part_letter else "",
                "版块3": block3,
            },
            "题目信息": {
                "背景信息": background_info,
                "上下文": context,
                "问题原文": question_text,
                "改造后问题": "",
                "核心思路": core_idea,
                "解答过程": solution_text,
                "最终答案": final_arr,
                "改造后答案": [""] * len(final_arr),
                "答案类型": [final_answer_type_val] if final_answer_type_val else [""],
                "改造后答案类型": [""] * len(final_arr),
                "答案单位": [answer_unit] if answer_unit else [""],
                "改造后答案单位": [""] * len(final_arr),
                "关联图片路径": [""] * len(final_arr),
                "模态类型": [modality] if modality else [""],
                "难度": difficulty,
            },
            "条件提取": {
                "显性条件": explicit_conditions if explicit_conditions else [],
                "隐性条件": implicit_conditions,
            },
            "物理模型": physical_model,
            "关联考点": related_kp,
            "物理场景": physical_scenario,
            "判分标准": rubric,
            "错误解题步骤": [],
            "多解法标注": [],
        }

        annotations.append(annotation)

        llm_tag = " [LLM]" if llm_result else ""
        todos.append(f"- [ ] Fill 改造后问题 for {sub_id}")
        todos.append(f"- [ ] Fill 改造后答案 for {sub_id}")
        todos.append(f"- [ ] Fill 改造后答案类型 for {sub_id}")
        todos.append(f"- [ ] Fill 改造后答案单位 for {sub_id}")
        if not related_kp:
            todos.append(f"- [ ] Fill 关联考点 for {sub_id}")
        if not physical_model:
            todos.append(f"- [ ] Fill 物理模型 for {sub_id}")
        if not physical_scenario:
            todos.append(f"- [ ] Fill 物理场景 for {sub_id}")
        if not rubric:
            todos.append(f"- [ ] Fill 判分标准 for {sub_id}")
        if not explicit_conditions and not implicit_conditions:
            todos.append(f"- [ ] Fill 条件提取 for {sub_id}")
        if not difficulty:
            todos.append(f"- [ ] Fill 难度 for {sub_id}")
        todos.append(f"- [ ] Fill 多解法标注 for {sub_id} (if applicable)")
        todos.append(f"- [ ] Fill 错误解题步骤 for {sub_id} (if applicable)")

    todos.extend([
        "- [ ] Manual physics correctness review",
        "- [ ] Verify all values are in English",
        "- [ ] Verify 关联图片路径 and 模态类型 per image",
    ])

    return annotations, todos


def main():
    if len(sys.argv) != 5:
        print("usage: fill_annotation_new.py <subquestions.json> <out.json> <review_todo.md> <meta.json>")
        sys.exit(1)
    sub_pack = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    meta = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
    annotations, todos = build_annotation_new(sub_pack, meta)
    Path(sys.argv[2]).write_text(json.dumps(annotations, indent=2, ensure_ascii=False), encoding="utf-8")
    Path(sys.argv[3]).write_text("# Review TODO\n\n## Manual items (after pipeline)\n" + "\n".join(todos) + "\n", encoding="utf-8")
    print(f"[fill_annotation_new] wrote {sys.argv[2]} and {sys.argv[3]}")


if __name__ == "__main__":
    main()
