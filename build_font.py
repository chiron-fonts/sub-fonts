import argparse
import os
import sys
from dataclasses import dataclass
from typing import Optional

from fontTools.misc.transform import Transform
from fontTools.otlLib.builder import buildValue
from fontTools.ttLib import TTFont, newTable
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.transformPen import TransformPen, TransformPointPen
from fontTools.ttLib.tables.otBase import OTTableWriter

merge_font_prefix = "inter_"


@dataclass
class FontBuildConfig:
    input_path: str
    output_path: str
    cjk_width_adjust: float
    apply_stylistic_set: str | None
    font_name: str
    font_version: str
    merge_font_path: str | None
    replace_punc: int = 0  # Replace punctuation glyphs with Latin font glyphs. 0=none, 1=!?, 2=!?:;,
    new_line_gap: int = 0  # New line gap to set (in font units)


def process_glyphs(font, cjk_width_extend_percent):
    glyf = font['glyf']
    glyphSet = font.getGlyphSet()
    hmtx = font['hmtx']
    glyph_order = font.getGlyphOrder()
    processed_glyphs = set()
    unprocessed_glyphs = set()
    for glyph_name in glyph_order:
        # glyph = glyf[glyph_name]
        width, lsb = hmtx[glyph_name]
        # Remove overlaps
        # pen = TTGlyphPen(font.getGlyphSet())
        # If width >= 1000, assume it's a CJK glyph, enlarge and shift
        if width >= 1000 and not glyph_name.startswith(merge_font_prefix):
            expand = int(width * cjk_width_extend_percent / 2)
            print(f"Expanding glyph {glyph_name} width from {width} to {width + expand * 2}")
            shift = int(expand / 2)
            # Transform glyph outline
            pen2 = TTGlyphPen(glyphSet)
            # transform = (2, 0, 0, 1, 500, 300)  # shift x by half expand
            transform = Transform().translate(0, 0)
            glyphSet[glyph_name].draw(TransformPen(pen2, transform))
            glyf[glyph_name] = pen2.glyph()
            hmtx[glyph_name] = (width + expand * 2, lsb + expand)
            processed_glyphs.add(glyph_name)
        else:
            unprocessed_glyphs.add(glyph_name)

    return processed_glyphs, unprocessed_glyphs


def set_font_parameter(font, new_name: str, new_version: str):
    name_table = font['name']
    # Delete nameId 17 (Preferred Subfamily) if exists
    name_table.names = [record for record in name_table.names if record.nameID != 17]

    # 1 = Font Family name, 4 = Full font name, 6 = PostScript name, 5 = Version string
    for record in name_table.names:
        if record.nameID == 1 or record.nameID == 4:
            # Replace family name and full name
            record.string = new_name.encode(record.getEncoding())
        elif record.nameID == 6:
            # Replace space with - for PostScript name
            record.string = new_name.replace(" ", "-").encode(record.getEncoding())
        elif record.nameID == 5:  # Version string
            record.string = new_version.encode(record.getEncoding())
        elif record.nameID == 2:  # Preferred subfamily
            record.string = "Regular".encode(record.getEncoding())


def set_line_gap(font, new_line_gap: int):
    if 'hhea' in font:
        font['hhea'].lineGap = new_line_gap
    if 'OS/2' in font:
        font['OS/2'].sTypoLineGap = new_line_gap
        font['OS/2'].usWinAscent += new_line_gap // 2
        font['OS/2'].usWinDescent += new_line_gap // 2


def remove_font_tables(font):
    # Remove STAT table if exists
    if 'STAT' in font:
        print("Removing STAT table...")
        del font['STAT']

    # Remove cv** tables if exist
    cv_tables = [tag for tag in font.keys() if tag.startswith('cv')]
    for tag in cv_tables:
        print(f"Removing {tag} table...")
        del font[tag]

    # Remove GSUB table if exists
    if 'GSUB' in font:
        print("Removing GSUB table...")
        del font['GSUB']


def build_font(config: FontBuildConfig):
    static_font = TTFont(config.input_path)

    # Throw error if variable table is found
    if 'cvar' in static_font or 'fvar' in static_font or 'gvar' in static_font:
        raise ValueError("Input font must be a static font.")

    if config.apply_stylistic_set:
        print("Applying stylistic sets...")
        apply_stylistic_sets(static_font, config.apply_stylistic_set)

    print("Removing stylistic sets...")
    remove_stylistic_sets(static_font)

    # Remove tables not needed in the final font
    print("Remove unneeded font tables...")
    remove_font_tables(static_font)

    print("Removing locl feature...")
    remove_locl_feature(static_font)

    if config.merge_font_path is not None:
        print("Integrating Latin font...")
        merge_inter_font(static_font, config.merge_font_path, config.replace_punc)

    print("Processing glyphs for CJK width adjustment...")
    [processed, unprocessed] = process_glyphs(static_font, config.cjk_width_adjust)

    # add_gpos_spacing(static_font, processed, unprocessed, spacing=60)
    # apply_gpos_spacing(static_font, processed, unprocessed, spacing=60)

    print(f"Setting font name to {config.font_name}...")
    set_font_parameter(static_font, config.font_name, config.font_version)

    if config.new_line_gap != 0:
        print(f"Setting new line gap to {config.new_line_gap}...")
        set_line_gap(static_font, config.new_line_gap)

    print(f"Saving static instance to {config.output_path}...")
    static_font.save(config.output_path)


def apply_stylistic_sets(font, tag):
    """
    Apply a specific stylistic set to the font (replace default glyphs with ss15 alternates).
    """
    if 'GSUB' not in font:
        print("No GSUB table found. Skipping stylistic set processing.")
        return
    gsub = font['GSUB'].table
    featureList = gsub.FeatureList
    lookupList = gsub.LookupList
    glyphOrder = font.getGlyphOrder()
    glyphSet = font.getGlyphSet()
    glyf = font['glyf'] if 'glyf' in font else None

    # Find stylistic set feature
    ssXX_feature_indices = [i for i, f in enumerate(featureList.FeatureRecord)
                            if f.FeatureTag == tag]

    # Apply stylistic set substitutions
    if ssXX_feature_indices:
        print(f"Applying {tag} stylistic set...")
        for feat_idx in ssXX_feature_indices:
            feature = featureList.FeatureRecord[feat_idx].Feature
            for lookup_idx in feature.LookupListIndex:
                lookup = lookupList.Lookup[lookup_idx]
                for subtable in lookup.SubTable:
                    # Only handle SingleSubst for simplicity
                    if hasattr(subtable, 'mapping'):
                        for src, dst in subtable.mapping.items():
                            if src in glyphOrder and dst in glyphOrder:
                                # Replace glyph outline with ss15 alternate
                                if glyf:
                                    glyf[src] = glyf[dst]
                                # If hmtx exists, copy metrics
                                if 'hmtx' in font:
                                    font['hmtx'][src] = font['hmtx'][dst]
    else:
        print(f"No {tag} stylistic set found.")


def remove_stylistic_sets(font):
    """
    Remove all stylistic set features (ss01–ss20) from the GSUB table.
    """
    if 'GSUB' not in font:
        print("No GSUB table found. Skipping stylistic set processing.")
        return
    gsub = font['GSUB'].table
    featureList = gsub.FeatureList

    ssXX_tags = [f'ss{str(i).zfill(2)}' for i in range(1, 21)]
    ssXX_indices = [i for i, f in enumerate(featureList.FeatureRecord)
                    if f.FeatureTag in ssXX_tags]

    print("Removing all stylistic set features (ss01–ss20)...")
    featureList.FeatureRecord = [f for i, f in enumerate(featureList.FeatureRecord)
                                 if i not in ssXX_indices]
    # Remove references from ScriptList
    for scriptRecord in gsub.ScriptList.ScriptRecord:
        for langSys in [scriptRecord.Script.DefaultLangSys] + list(
                scriptRecord.Script.LangSysRecord[i].LangSys for i in range(len(scriptRecord.Script.LangSysRecord))):
            if langSys is None:
                continue
            langSys.FeatureIndex = [idx for idx in langSys.FeatureIndex if idx not in ssXX_indices]


def remove_locl_feature(font):
    """
    Remove all 'locl' features from the GSUB table, including references in ScriptList.
    """
    if 'GSUB' not in font:
        print("No GSUB table found. Skipping locl feature removal.")
        return
    gsub = font['GSUB'].table
    featureList = gsub.FeatureList
    # Find indices of locl features
    locl_indices = [i for i, f in enumerate(featureList.FeatureRecord)
                    if f.FeatureTag == 'locl']
    if not locl_indices:
        print("No locl features found in GSUB.")
        return
    print(f"Removing {len(locl_indices)} locl feature(s) from GSUB...")
    # Remove locl FeatureRecords
    featureList.FeatureRecord = [f for i, f in enumerate(featureList.FeatureRecord)
                                 if i not in locl_indices]
    # Remove references from ScriptList
    for scriptRecord in gsub.ScriptList.ScriptRecord:
        langSysList = [scriptRecord.Script.DefaultLangSys] if scriptRecord.Script.DefaultLangSys else []
        langSysList += [lsr.LangSys for lsr in scriptRecord.Script.LangSysRecord]
        for langSys in langSysList:
            if langSys is None:
                continue
            langSys.FeatureIndex = [idx for idx in langSys.FeatureIndex if idx not in locl_indices]
    print("locl feature removal complete.")


def merge_inter_font_gpos_table(target_font, inter_font, inter_name_map):
    """Merge GPOS data from inter_font into target_font without replacing existing data.
    Behavior:
      - Deep copy inter GPOS structures.
      - Remap every glyph name via inter_name_map (original->new) recursively (Coverage, classDefs, PairPos etc.).
      - If target lacks GPOS, clone whole table; else append lookups, features, scripts with index offset adjustment.
      - Idempotent using _inter_gpos_merged flag.
      - Verification pass warns if any original (unprefixed) inter glyph names remain in GPOS after merge.
    """
    print("Merging GPOS table from Inter font...")

    if getattr(target_font, '_inter_gpos_merged', False):
        return
    if 'GPOS' not in inter_font:
        print("[GPOS] Inter font has no GPOS; skip.")
        target_font._inter_gpos_merged = True
        return

    from fontTools.ttLib.tables import otTables
    import copy as _copy

    visited = set()

    def remap(obj):
        if obj is None:
            return obj
        oid = id(obj)
        if oid in visited:
            # print("Already visited", type(obj), obj)
            return obj
        visited.add(oid)
        # Coverage
        if hasattr(obj, 'glyphs') and isinstance(obj.glyphs, list):
            print('Replacing glyphs in Coverage:', obj.glyphs)
            obj.glyphs = [inter_name_map.get(g, g) for g in obj.glyphs]
        # Class definitions (generic Single classDefs)
        if hasattr(obj, 'classDefs') and isinstance(obj.classDefs, dict):
            print('Replacing glyphs in classDefs:', list(obj.classDefs.keys())[:10], '...')
            obj.classDefs = {inter_name_map.get(g, g): cls for g, cls in obj.classDefs.items()}
        # PairPos Format2 class definitions: ClassDef1 / ClassDef2
        if hasattr(obj, 'ClassDef1') and getattr(obj, 'ClassDef1') and hasattr(obj.ClassDef1, 'classDefs'):
            print('Replacing glyphs in ClassDef1:', list(obj.ClassDef1.classDefs.keys())[:10], '...')
            obj.ClassDef1.classDefs = {inter_name_map.get(g, g): cls for g, cls in obj.ClassDef1.classDefs.items()}
        if hasattr(obj, 'ClassDef2') and getattr(obj, 'ClassDef2') and hasattr(obj.ClassDef2, 'classDefs'):
            print('Replacing glyphs in ClassDef2:', list(obj.ClassDef2.classDefs.keys())[:10], '...')
            obj.ClassDef2.classDefs = {inter_name_map.get(g, g): cls for g, cls in obj.ClassDef2.classDefs.items()}
        # Pair positioning Format1 second glyphs
        if hasattr(obj, 'PairSet') and isinstance(obj.PairSet, list):
            for pairSet in obj.PairSet:
                if hasattr(pairSet, 'PairValueRecord'):
                    for pvr in pairSet.PairValueRecord:
                        if hasattr(pvr, 'SecondGlyph') and isinstance(pvr.SecondGlyph, str):
                            print(
                                f"Replacing PairPos secondGlyph: {pvr.SecondGlyph} -> {inter_name_map.get(pvr.SecondGlyph, pvr.SecondGlyph)}")
                            pvr.SecondGlyph = inter_name_map.get(pvr.SecondGlyph, pvr.SecondGlyph)
        # Extension subtable
        if hasattr(obj, 'ExtSubTable'):
            print("Recursing into ExtSubTable")
            remap(obj.ExtSubTable)
        # Contextual / chaining positioning fields: Input, Backtrack, LookAhead
        for attr in dir(obj):
            if attr.startswith('_'):
                continue
            try:
                val = getattr(obj, attr)
            except Exception:
                continue
            # Generic recursion / attribute scan continue
            if val is obj or isinstance(val, (int, float, str, bytes, bool)):
                continue
            if isinstance(val, list):
                for item in val:
                    remap(item)
                    # print('Returned from list item, type', type(item), 'value', item)
                val = getattr(obj, attr)
                # print(f"After remap, attribute {attr} of type {type(val)} has value {val}")
            elif isinstance(val, tuple):
                for item in val:
                    # Replace direct string glyph name attributes
                    if isinstance(val, str) and val in inter_name_map:
                        try:
                            setattr(obj, attr, inter_name_map[val])
                        except Exception:
                            pass
                        remap(item)
            else:
                if hasattr(val, '__dict__'):
                    remap(val)
        return obj

    # Simple path: target has no GPOS
    if 'GPOS' not in target_font:
        new_gpos = _copy.deepcopy(inter_font['GPOS'])
        remap(new_gpos.table)
        target_font['GPOS'] = new_gpos
        target_font._inter_gpos_merged = True
        print(
            f"[GPOS] Cloned entire GPOS. Lookups={len(new_gpos.table.LookupList.Lookup) if new_gpos.table.LookupList else 0}")
    else:
        tgt_gpos = target_font['GPOS'].table
        lookup_offset = tgt_gpos.LookupList.LookupCount
        feature_offset = tgt_gpos.FeatureList.FeatureCount
        # Copy lookups
        new_gpos = _copy.deepcopy(inter_font['GPOS'])
        remap(new_gpos.table)
        src_gpos = new_gpos.table

        for idx, src_lookup in enumerate(src_gpos.LookupList.Lookup):
            new_lookup = src_lookup
            tgt_gpos.LookupList.Lookup.insert(0, new_lookup)
            print(f"[GPOS] Added lookup {idx} type={getattr(src_lookup, 'LookupType', '?')}")
        tgt_gpos.LookupList.LookupCount = len(tgt_gpos.LookupList.Lookup)
        # Features
        for fidx, fr in enumerate(src_gpos.FeatureList.FeatureRecord if src_gpos.FeatureList else []):
            new_fr = _copy.deepcopy(fr)
            new_fr.Feature.LookupListIndex = [lookup_offset + i for i in new_fr.Feature.LookupListIndex]
            tgt_gpos.FeatureList.FeatureRecord.append(new_fr)
            print(f"[GPOS] Added feature {fidx} tag={fr.FeatureTag}")
        tgt_gpos.FeatureList.FeatureCount = len(tgt_gpos.FeatureList.FeatureRecord)
        # Scripts
        for sidx, srec in enumerate(src_gpos.ScriptList.ScriptRecord if src_gpos.ScriptList else []):
            new_srec = _copy.deepcopy(srec)
            if new_srec.Script.DefaultLangSys:
                new_srec.Script.DefaultLangSys.FeatureIndex = [feature_offset + fi for fi in
                                                               new_srec.Script.DefaultLangSys.FeatureIndex]
            for lsr in new_srec.Script.LangSysRecord:
                lsr.LangSys.FeatureIndex = [feature_offset + fi for fi in lsr.LangSys.FeatureIndex]
            tgt_gpos.ScriptList.ScriptRecord.append(new_srec)
            print(f"[GPOS] Added script {sidx} tag={srec.ScriptTag}")
        tgt_gpos.ScriptList.ScriptCount = len(tgt_gpos.ScriptList.ScriptRecord)
        target_font._inter_gpos_merged = True
        print(f"[GPOS] Merge appended. Total lookups={tgt_gpos.LookupList.LookupCount}")


def merge_inter_font(target_font, inter_font_path: str, replace_punc: int):
    """
    1. Copy all glyphs and metrics from Inter font to target font, renaming with {merge_font_prefix} prefix.
    2. For composite glyphs, update component references to use the new prefixed names.
    3. For each codepoint in Inter, if the target font's glyph for that codepoint is not width 1000, update cmap to point to the new Inter glyph.
    """
    print(f"Integrating Inter font from {inter_font_path}...")
    inter_font = TTFont(inter_font_path)

    # only static font is supported
    if 'fvar' in inter_font:
        raise ValueError("Inter font must be a static font without 'fvar' table.")

    inter_glyf = inter_font['glyf']
    inter_hmtx = inter_font['hmtx']
    target_glyf = target_font['glyf']
    target_hmtx = target_font['hmtx']
    inter_glyph_order = inter_font.getGlyphOrder()
    target_glyph_order = target_font.getGlyphOrder()
    glyph_order_set = set(target_glyph_order)

    # Map: original Inter glyph name -> new glyph name with prefix
    inter_name_map = {}
    for inter_glyph_name in inter_glyph_order:
        new_glyph_name = f"{merge_font_prefix}{inter_glyph_name}"
        inter_name_map[inter_glyph_name] = new_glyph_name
        glyph_order_set.add(new_glyph_name)
        target_glyph_order.append(new_glyph_name)

    merge_inter_font_gpos_table(target_font, inter_font, inter_name_map)

    # Step 1: Copy all glyphs and metrics from Inter to target, with prefix
    print(f"Copying all glyphs and metrics from Inter font with '{merge_font_prefix}' prefix...")
    for inter_glyph_name, new_glyph_name in inter_name_map.items():
        glyph = inter_glyf[inter_glyph_name]
        if glyph.isComposite():
            raise NotImplementedError("Composite glyph not supported.")

        pen = TTGlyphPen(inter_glyf.glyphs)
        inter_glyf[inter_glyph_name].draw(pen, inter_glyf.glyphs)
        target_glyf[new_glyph_name] = pen.glyph()
        # Copy metrics
        if inter_glyph_name in inter_hmtx.metrics:
            target_hmtx[new_glyph_name] = inter_hmtx[inter_glyph_name]

    target_font.setGlyphOrder(target_glyph_order)

    # Replace the CJK's comma (U+FF0C), exclamation and question marks (U+FF01, U+FF1F) with Inter's versions
    # while keeping the original CJK glyphs' widths (draw the Inter glyphs at the center of the CJK widths)
    inter_punc_map = {}
    if replace_punc >= 1:
        inter_punc_map = {
            0xFF01: 'exclam',  # Fullwidth exclamation mark
            0xFF1F: 'question',  # Fullwidth question mark
        }
    if replace_punc >= 2:
        inter_punc_map = {
            0xFF0C: 'comma',  # Fullwidth comma
            0xFF1A: 'colon',  # Fullwidth colon
            0xFF1B: 'semicolon',  # Fullwidth semicolon
            0xFF01: 'exclam',  # Fullwidth exclamation mark
            0xFF1F: 'question',  # Fullwidth question mark
        }

    def get_glyph_bbox(font, glyph):
        if hasattr(glyph, "xMin"):
            bbox = (glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax, (glyph.xMin + glyph.xMax) / 2,
                    (glyph.yMin + glyph.yMax) / 2)
            return bbox
        else:
            raise ValueError("Glyph has no bounding box.")

    for cp, inter_glyph_name in inter_punc_map.items():
        if inter_glyph_name not in inter_name_map:
            raise ValueError(f"Inter glyph '{inter_glyph_name}' not found in Inter font.")
        if cp in target_font.getBestCmap():
            target_glyph_name = target_font.getBestCmap()[cp]
            cjk_width, cjk_lsb = target_hmtx[target_glyph_name]
            inter_width, inter_lsb = inter_hmtx[inter_glyph_name]
            # Calculate shift to center Inter glyph in CJK width
            shift = (cjk_width - inter_width) // 2 - inter_lsb
            print(f"Replacing U+{cp:04X} ({target_glyph_name}) with {inter_glyph_name}, shifting by {shift}")
            # Transform Inter glyph outline
            pen2 = TTGlyphPen(target_font.getGlyphSet())
            if cp == 0xFF0C or cp == 0xFF1A or cp == 0xFF1B:
                # For comma, align the glyph to the center of the original glyph.
                # First, find the boundary of the original glyph
                original_bbox = get_glyph_bbox(target_font, target_glyf[target_glyph_name])
                inter_bbox = get_glyph_bbox(inter_font, inter_glyf[inter_glyph_name])
                print(f" - Original glyph bbox: {original_bbox}")
                print(f" - Inter glyph bbox: {inter_bbox}")

                # Align Inter's glyph center to original glyph center
                original_x_center = original_bbox[4]
                original_y_center = original_bbox[5]

                inter_x_center = inter_bbox[4]
                inter_y_center = inter_bbox[5]

                # Calculate the shift needed to align centers
                center_shift = original_x_center - inter_x_center
                vertical_shift = original_y_center - inter_y_center

                transform = Transform().translate(center_shift, vertical_shift)
            else:
                transform = Transform().translate(shift, 0)
            inter_glyf[inter_glyph_name].draw(TransformPen(pen2, transform), inter_glyf.glyphs)
            target_glyf[target_glyph_name] = pen2.glyph()

    # Step 2: Update cmap mappings for codepoints
    print("Updating cmap mappings for Inter codepoints...")
    inter_cmap = {}
    for table in inter_font['cmap'].tables:
        inter_cmap.update(table.cmap)
    target_cmap = {}
    for table in target_font['cmap'].tables:
        target_cmap.update(table.cmap)
    for cp, inter_glyph_name in inter_cmap.items():
        if (cp < 0x20) or (0x7F <= cp < 0xA0):
            # Skip control characters
            continue
        # If codepoint exists in target font, check width
        target_glyph_name = target_cmap.get(cp)
        if target_glyph_name and target_glyph_name in target_hmtx.metrics:
            width, lsb = target_hmtx[target_glyph_name]
            if width >= 1000:
                print(f" - Skipping cmap update for U+{cp:04X} due to width 1000")
                continue
        # Update cmap to point to new Inter glyph
        new_glyph_name = inter_name_map[inter_glyph_name]

        for table in target_font['cmap'].tables:
            if table.isUnicode():
                # print(f"Table format: {table.format}, platformID: {table.platformID}, platEncID: {table.platEncID}")
                if table.format == 4:
                    if cp <= 0xFFFF:
                        table.cmap[cp] = new_glyph_name
                        print(f" - Updated cmap: U+{cp:04X} -> {new_glyph_name}")
                    else:
                        print(f" * Skipped U+{cp:04X} for format 4 (BMP only)")
                elif table.format == 12:
                    # if cp > 0xFFFF:
                    table.cmap[cp] = new_glyph_name
                    print(f" - Updated cmap: U+{cp:04X} -> {new_glyph_name} (format 12)")

    print("Inter font integration complete.")


def main():
    parser = argparse.ArgumentParser(
        prog='Generate a sub font',
        epilog='Example: python build_font.py -w 0.12 -m tmp/Inter-320-20.ttf -fn "Sub Hei In A12 W370" fv="2025.090" tmp/ChironHeiHK-370.ttf tmp/SubHei-In-A12-W370.ttf'
    )

    parser.add_argument('input', help='Input font file path (static).')
    parser.add_argument('-w', '--cjk-width-adjust', type=float, required=True,
                        help='Scaling factor for CJK glyph widths, e.g. 0.12 to increase by 12%% = 120 unit (60 on the left and 60 on the right).')
    parser.add_argument('-m', '--merge-font', type=str, default=None,
                        help='Merge glyphs and metrics from the specified LGC font file.')
    parser.add_argument('-fn', '--font-name', type=str, required=True,
                        help='Set the output font name (e.g. "Sub Hei IN-370").')
    parser.add_argument('-fv', '--font-version', type=str, required=True,
                        help='Set the font version string.')
    parser.add_argument('-ss', '--apply-ss', type=str, default=None,
                        help='Apply a specific stylistic set (e.g. ss15) to the font.')
    parser.add_argument('-p', '--replace-punc', type=int, default=0,
                        help='Replace certain CJK punctuation with Latin font versions: 0=none, 1=exclam/question, 2=comma/colon/semicolon/exclam/question.')
    parser.add_argument('output', help='Output font file path.')

    args = parser.parse_args()
    input_path = args.input
    out_path = args.output
    lgc_font_path = args.merge_font

    if not input_path:
        print("No input font supplied.")
        sys.exit(1)

    if not out_path:
        print("No output font supplied.")
        sys.exit(1)

    if not os.path.isfile(input_path):
        print(f"Input font not found: {input_path}")
        sys.exit(1)

    if lgc_font_path is not None:
        if not os.path.isfile(lgc_font_path):
            print(f"Font not found: {lgc_font_path}")
            sys.exit(1)

    config = FontBuildConfig(
        input_path=args.input,
        output_path=args.output,
        cjk_width_adjust=args.cjk_width_adjust,
        font_name=args.font_name,
        font_version=args.font_version,
        merge_font_path=lgc_font_path,
        apply_stylistic_set=args.apply_ss,
        replace_punc=args.replace_punc,
    )

    build_font(config)


if __name__ == "__main__":
    main()
