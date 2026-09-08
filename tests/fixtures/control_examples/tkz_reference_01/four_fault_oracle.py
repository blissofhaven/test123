"""Independent RMS oracle: nameplate inputs -> six complex phase equations.

No RZA imports, supplied expected values, or scalar fault-current formulae.
Requires Python 3.10+ and NumPy. Importing this module has no side effects.
"""
import argparse
import cmath
import hashlib
import json
import math
from pathlib import Path

import numpy as np

A120 = cmath.exp(2j * math.pi / 3)
PHASE = np.array([[1, 1, 1], [1, A120**2, A120],
                  [1, A120, A120**2]], dtype=complex)
SEQUENCE = np.linalg.inv(PHASE)
FAULTS = ('3ph', 'LL_BC', 'LG_A', 'LLG_BCG_common')


def pairs(values):
    return [[float(z.real), float(z.imag)] for z in values]


def phase_solve(z1, z2, z0, emf_v, fault, *, zero_path):
    """Solve x=(Va,Vb,Vc,Ia,Ib,Ic), voltages V and currents A.

    known_open means an explicitly ideal isolated network with no capacitance.
    It never represents missing sequence data. LL then needs a V0 gauge.
    """
    if zero_path not in ('known_open', 'finite_impedance'):
        raise ValueError('Missing/unknown zero-sequence topology is not an oracle input')
    if (z0 is None) != (zero_path == 'known_open'):
        raise ValueError('zero_path and z0 disagree')
    matrix = np.zeros((6, 6), complex)
    rhs = np.zeros(6, complex)
    if zero_path == 'known_open':
        matrix[0, 3:] = SEQUENCE[0]  # I0 = 0; neutral potential may float.
    else:
        matrix[0, :3], matrix[0, 3:] = SEQUENCE[0], z0 * SEQUENCE[0]
    for row, z, emf in ((1, z1, emf_v), (2, z2, 0)):
        matrix[row, :3], matrix[row, 3:] = SEQUENCE[row], z * SEQUENCE[row]
        rhs[row] = emf
    if fault == '3ph':
        matrix[3:, :3] = np.eye(3)  # Va = Vb = Vc = 0.
    elif fault == 'LL_BC':
        matrix[3, 3] = 1  # Ia = 0.
        matrix[4, 4:] = 1  # Ib + Ic = 0.
        matrix[5, 1:3] = (1, -1)  # Vb = Vc.
        if zero_path == 'known_open':
            matrix[4] = 0
            matrix[4, :3] = SEQUENCE[0]  # V0=0 gauge; current sum already zero.
    elif fault == 'LG_A':
        matrix[3, 4], matrix[4, 5], matrix[5, 0] = 1, 1, 1
    elif fault == 'LLG_BCG_common':
        matrix[3, 3], matrix[4, 1], matrix[5, 2] = 1, 1, 1
    else:
        raise ValueError(f'Unknown fault: {fault}')
    v, i = np.split(np.linalg.solve(matrix, rhs), 2)
    vs, iss = SEQUENCE @ v, SEQUENCE @ i
    # Check physical equations directly, independently of matrix row assembly.
    voltage_errors = [vs[1] + z1 * iss[1] - emf_v, vs[2] + z2 * iss[2]]
    current_errors = list(PHASE @ iss - i)
    if zero_path == 'known_open':
        current_errors.append(iss[0])
    else:
        voltage_errors.append(vs[0] + z0 * iss[0])
    if fault == '3ph':
        voltage_errors.extend(v)
    elif fault == 'LL_BC':
        current_errors.extend((i[0], i[1] + i[2]))
        voltage_errors.append(v[1] - v[2])
    elif fault == 'LG_A':
        current_errors.extend(i[1:])
        voltage_errors.append(v[0])
    else:
        current_errors.append(i[0])
        voltage_errors.extend(v[1:])
    err_a = float(max(map(abs, current_errors)))
    err_v = float(max(map(abs, voltage_errors)))
    checks = {'current_boundary_error_A': err_a, 'voltage_boundary_error_V': err_v,
              'passed': err_a <= 1e-8 + 1e-11 * max(map(abs, i))
                        and err_v <= 1e-8 + 1e-11 * abs(emf_v)}
    if not checks['passed']:
        raise ArithmeticError(checks)
    return {'Iabc_kA': pairs(i / 1000), 'I012_kA': pairs(iss / 1000),
            'Vabc_V': pairs(v), 'V012_V': pairs(vs),
            'phase_magnitudes_kA': list(map(float, abs(i) / 1000)),
            'max_phase_kA': float(max(abs(i)) / 1000),
            'residual_3I0_kA': float(abs(sum(i)) / 1000), 'checks': checks}


def impedances(data):
    """Derive unrounded R+jX exclusively from the independent input section."""
    src = data['inputs']
    u1, u2, u3 = (src['voltage_kv'][key] for key in ('U1', 'U2', 'U3'))
    omega = 2 * math.pi * src['frequency_hz']
    gen = src['generator']
    if gen['rated_kv'] != u1:
        raise ValueError('This fixture requires the declared generator voltage to equal U1')
    zero_t = src['assumed_zero_equivalents']['T2']
    if zero_t != {'Z0_equals_Z1': True, 'neutral_impedance_ohm': 0}:
        raise ValueError('This bounded fixture supports only the explicitly assumed T2 zero equivalent')
    x1 = gen['xd_subtransient_pu'] * u1**2 / gen['rated_mva']
    rg = x1 / (omega * gen['dc_time_constant_s'])
    zg1 = complex(rg, x1)
    zg2 = complex(rg, gen['negative_reactance_pu'] * u1**2 / gen['rated_mva'])
    def transformer(t, kv):
        z = t['uk_percent'] / 100 * kv**2 / t['rated_mva']
        r = t['loss_kw'] / 1000 * kv**2 / t['rated_mva']**2
        return complex(r, math.sqrt(z*z - r*r))
    t1 = transformer(src['transformers']['T1'], u2)
    t2 = transformer(src['transformers']['T2'], u3)
    lines = {key: complex(x['r_positive_ohm_km'], x['x_positive_ohm_km'])
             * x['length_km'] for key, x in src['lines'].items()}
    line_zero = src['assumed_zero_equivalents']['L3']
    l3zero = complex(line_zero['r0_over_r1'] * lines['L3'].real,
                     line_zero['x0_over_x1'] * lines['L3'].imag)
    for mode, state in src['modes'].items():
        n = len(state['online_generator_ids'])
        z16, z26 = (z / n * (u2/u1)**2 + t1 for z in (zg1, zg2))
        z13, z23 = z16 + lines['L1'], z26 + lines['L1']
        z14, z24 = ((z + lines['L2']) * (u3/u2)**2 + t2 for z in (z13, z23))
        points = {'K1': (zg1/n, zg2/n, None, u1), 'K2': (z16, z26, None, u2),
                  'K3': (z13, z23, None, u2), 'K4': (z14, z24, t2, u3),
                  'K5': (z14 + lines['L3'], z24 + lines['L3'], t2 + l3zero, u3)}
        for point, (z1, z2, z0, kv) in points.items():
            yield mode, point, z1, z2, z0, gen['emf_subtransient_pu'] * kv * 1000/math.sqrt(3)


def build_reference(data):
    cases = []
    for mode, point, z1, z2, z0, emf in impedances(data):
        path = 'known_open' if z0 is None else 'finite_impedance'
        for fault in FAULTS:
            cases.append({'id': f'{mode}/{point}/{fault}', 'mode': mode, 'point': point,
                'fault': fault, 'zero_path': path,
                'scope': ('conditional_ideal_isolated_no_capacitance' if z0 is None
                          and fault in ('LG_A', 'LLG_BCG_common') else 'stated_sequence_equivalent'),
                'emf_positive_V': [emf, 0.0],
                'Z012_ohm': [None if z0 is None else pairs([z0])[0], *pairs([z1, z2])],
                'expected': phase_solve(z1, z2, z0, emf, fault, zero_path=path)})
    if len(cases) != 40 or len({x['id'] for x in cases}) != 40:
        raise ValueError('The supplied reference must have 2 modes × 5 points × 4 faults')
    return {'schema': 'rza-independent-phase-oracle/1', 'provenance': data['provenance'],
            'assumptions': data['assumptions'], 'units': {'currents': 'kA RMS',
            'voltages': 'V RMS', 'impedances': 'ohm', 'complex': '[real,imag]',
            'phase_order': 'A,B,C', 'sequence_order': '0,1,2'},
            'regression_tolerance': {'relative': 1e-9, 'current_absolute_kA': 1e-9,
                                     'voltage_absolute_V': 1e-6}, 'cases': cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=Path(__file__).with_name('testcase_inputs.json'))
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('testcase_reference.json'))
    args = parser.parse_args()
    raw = args.inputs.read_bytes()
    result = build_reference(json.loads(raw))
    result['input_sha256'] = hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(result["cases"])} cases; all physical boundary checks passed; {args.output}')


if __name__ == '__main__':
    main()
