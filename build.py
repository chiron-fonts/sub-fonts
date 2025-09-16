import argparse
import os
import re
from pathlib import Path
from build_font import build_font, FontBuildConfig
from extract_font import extract_font
from fontTools.misc.transform import Transform
import yaml


def load_config(path):
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def flatten_axis(axis_settings):
    return ','.join(f'{k}={v}' for k, v in axis_settings.items())


def flatten_transform(transform: Transform):
    if transform is None:
        return '0'
    return f"{int(transform.xx * 100)},{int(transform.xy * 100)},{int(transform.yx * 100)},{int(transform.yy * 100)},{int(transform.dx)},{int(transform.dy)}"


def replace_placeholders(template, params):
    return re.sub(r'\[\[(\w+)\]\]', lambda m: str(params.get(m.group(1), m.group(0))), template)


def get_version_code(version):
    return f'{version[0:2]}{version[3:5]}'


def run_build_from_config(config, version, fonts_dir, tmp_dir, output_dir):
    os.makedirs(tmp_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    name_params = {'version': get_version_code(version)}
    profiles = config.get('profiles', [])
    for profile in profiles:
        src = profile.get('font')
        src_latin = profile.get('latin_font')
        latin_code = profile.get('latin_font_code')
        build_default_latin = profile.get('build_default_latin', False)
        name = profile.get('name')
        configurations = profile.get('configurations', {})
        weights = configurations.get('weights', [])
        expand_widths = configurations.get('expand_widths', [])
        latin_replace_punc = configurations.get('latin_replace_punc', [None])
        apply_ss = configurations.get('apply_ss', [None])
        new_line_gap = configurations.get('new_line_gap', 0)

        src = os.path.join(fonts_dir, src)
        src_latin = os.path.join(fonts_dir, src_latin) if src_latin else None

        if not os.path.exists(src):
            raise FileNotFoundError(f"CJK font file {src} does not exist.")

        if src_latin is not None and not os.path.exists(src_latin):
            raise FileNotFoundError(f"Latin font file {src_latin} does not exist.")

        name_params['latn_code'] = latin_code if latin_code is not None else 'DF'

        for weight in weights:
            cjk_axis = weight.get('axis')
            cjk_transform = Transform(
                *[float(x) for x in
                  str(weight.get('transform')).split(',')]) if weight.get('transform') is not None else None

            latin_axis = weight.get('latin_axis')
            latin_transform = Transform(
                *[float(x) for x in
                  str(weight.get('latin_transform')).split(',')]) if weight.get('latin_transform') is not None else None

            name_params['wght'] = cjk_axis.get('wght')
            name_params['cjk_scalex_inv'] = f"{int(100 - cjk_transform.xx * 100)}" if cjk_transform is not None else 0

            # Extract Latin font if specified
            if src_latin is not None and latin_axis:
                latin_out = f"{tmp_dir}/{Path(src_latin).stem}-{flatten_axis(latin_axis)}-{flatten_transform(latin_transform)}.ttf"
                print(f"Extracting Latin font {src_latin} to {latin_out}...")
                # Only run if file does not exist
                if not os.path.exists(latin_out):
                    extract_font(
                        input_path=src_latin,
                        output_path=latin_out,
                        axis_settings=latin_axis,
                        transform=latin_transform
                    )
            else:
                latin_out = None

            # Extract CJK font
            cjk_out = f"{tmp_dir}/{name}-{flatten_axis(cjk_axis)}-{flatten_transform(cjk_transform)}.ttf"
            if not os.path.exists(cjk_out):
                print(f"Extracting font {src} to {cjk_out}...")
                extract_font(
                    input_path=src,
                    output_path=cjk_out,
                    axis_settings=cjk_axis,
                    skip_decomposition=True,
                    transform=cjk_transform,
                    cjk_mode_transform=True
                )
            else:
                print(f"Skipping extraction of {cjk_out}, file already exists.")

            for expand_width in expand_widths:
                width_str = f"{int(expand_width * 100):02d}"
                name_params['advance_inc'] = width_str
                for replace_punc in latin_replace_punc:
                    name_params['replace_punc'] = replace_punc if replace_punc is not None else '0'
                    for k in apply_ss:
                        ss = apply_ss[k]
                        name_params['ss_mode'] = f"{k}"

                        # Generate a version without LGC glyphs replaced
                        if latin_out is not None and build_default_latin:
                            no_latin_name_params = name_params.copy()
                            no_latin_name_params['latn_code'] = 'DF'
                            font_name = replace_placeholders(name, no_latin_name_params)
                            print(f'**** BUILDING {font_name} ****')
                            build_font(FontBuildConfig(
                                input_path=cjk_out,
                                output_path=f'{output_dir}/{font_name}.ttf',
                                cjk_width_adjust=expand_width,
                                apply_stylistic_set=f'ss{ss}' if ss is not None else None,
                                font_name=font_name,
                                font_version=version,
                                new_line_gap=new_line_gap,
                                merge_font_path=None,
                            ))

                        # replace name with params
                        font_name = replace_placeholders(name, name_params)
                        print(f'**** BUILDING {font_name} ****')
                        build_font(FontBuildConfig(
                            input_path=cjk_out,
                            output_path=f'{output_dir}/{font_name}.ttf',
                            cjk_width_adjust=expand_width,
                            apply_stylistic_set=f'ss{ss}' if ss is not None else None,
                            font_name=font_name,
                            font_version=version,
                            merge_font_path=latin_out,
                            replace_punc=replace_punc,
                            new_line_gap=new_line_gap,
                        ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog='Run the font build routine',
        epilog='Example: python build.py sung 25.091 build/'
    )

    parser.add_argument('profile', type=str, help='Profile file name.')
    parser.add_argument('version', type=str, help='Version string, must be in format YY.MMp. (e.g. 25.091)')
    parser.add_argument('-o', '--output', type=str, default='/build/',
                        help='Output directory for built fonts. Defaults to /build/.')
    parser.add_argument('-f', '--fonts-dir', type=str, default='/fonts/',
                        help='Where to find the source fonts. Defaults to /fonts/.')
    parser.add_argument('-t', '--tmp-dir', type=str, default='/tmp/',
                        help='Temporary directory for intermediate files. Defaults to /tmp/.')

    args = parser.parse_args()

    # Print help if validation fails
    if not re.match(r'^\d{2}\.\d{3}$', args.version):
        parser.print_help()
        print('Error: Version string must be in format YY.MMp. (e.g. 25.091)')
        exit(1)

    print(f'Using profile: {args.profile}, version: {args.version} (Version code {get_version_code(args.version)}), fonts dir: {args.fonts_dir}, tmp dir: {args.tmp_dir}, output dir: {args.output}')

    config_path = os.path.join(os.path.dirname(__file__), f'profiles/{args.profile}.yaml')
    if not os.path.exists(config_path):
        print(f'Profile file {config_path} does not exist.')
        parser.print_help()
        exit(1)

    config = load_config(config_path)
    version = args.version
    run_build_from_config(config, version, args.fonts_dir, args.tmp_dir, args.output)
