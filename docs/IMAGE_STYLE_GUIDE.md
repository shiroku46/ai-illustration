# Image style guide

## Status notation

- **Confirmed:** required visual direction.
- **Recommended:** operational guidance to test and refine.
- **Unresolved:** final identity decision reserved for human review.

## Target style

**Confirmed.** The target is a flat, hand-drawn, deliberately rough character-illustration style with minimal shading. The result should feel authored rather than mechanically perfected. It may use controlled simplification, asymmetry, uneven proportions, and expressive distortion while remaining readable and consistent.

The goal is not to imitate defects randomly. Every irregularity should support character, gesture, rhythm, or readability.

## Core visual principles

1. Prefer a clear silhouette over intricate rendering.
2. Use line variation and imperfect contour rhythm rather than uniform digital inking.
3. Preserve readable character identity across expressions and poses.
4. Allow asymmetry that feels intentional.
5. Keep colors flat and limited; use shading sparingly.
6. Avoid generic anime-face construction shared by both characters.
7. Treat hands, limbs, and posture as acting tools, not filler anatomy.
8. Reject technically clean images when they feel templated, over-balanced, or stylistically anonymous.

## Line guidance

### Accept

- Mild wobble, taper changes, broken or restarted strokes, and selective line weight.
- Some contours drawn more emphatically than internal detail.
- Small differences between mirrored features.
- Simplified folds and hair masses.
- Occasional open contours where readability is preserved.

### Reject

- Identical line thickness across face, hair, clothing, hands, and silhouette.
- Perfectly smooth vector-like contours without intentional contrast.
- Repeated parallel detail lines with mechanical spacing.
- Excessive tiny hair strands or fabric details.
- Line noise that does not correspond to form or gesture.

### Review checks

- At thumbnail size, does the silhouette still read?
- Do important contours have stronger emphasis than minor details?
- Are repeated strokes too evenly spaced?
- Does the line feel authored rather than filtered?

## Silhouette and proportions

### Accept

- Distinct body and costume shapes for boke and tsukkomi.
- Controlled head/body ratio differences.
- Hands or feet enlarged when they strengthen performance.
- Uneven shoulder, hip, arm, or stance relationships that support the pose.
- Clear negative space between arms and torso.

### Reject

- Both characters sharing the same body template with recolored details.
- Perfect bilateral symmetry in neutral or emotional poses.
- Anatomical distortions that confuse limb ownership or joint direction.
- Tiny hands, hidden hands, or generic hands used to avoid acting decisions.
- Mechanically centered weight distribution in every pose.

## Face and eyes

### Accept

- Character-specific eye shape, spacing, eyelid treatment, eyebrow rhythm, and mouth construction.
- Small left/right differences that preserve expression.
- Simplified pupils and highlights appropriate to flat rendering.
- Expression driven by the whole face, not only mouth curvature.
- Different resting expressions for the two roles.

### Reject

- Large glossy generic anime eyes with identical highlight patterns on both characters.
- Perfectly mirrored eyes and eyebrows.
- Excessive iris gradients, glass-like reflections, or polished beauty rendering.
- Face shapes that change identity between variants.
- Expressions formed by swapping only a mouth sticker.

### Identity anchors

Each approved character specification should eventually define:

- face silhouette;
- eye shape and spacing;
- eyebrow style;
- nose simplification;
- mouth width and default angle;
- hairline and major hair masses;
- one or more costume or accessory anchors.

These anchors are **unresolved** until human-approved designs exist.

## Hands and limbs

### Accept

- Simplified finger groups when the gesture remains clear.
- Exaggerated pointing, open-palm, folded-arm, or recoil gestures.
- Nonuniform limb lengths when intentionally stylized and consistent.
- Hands crossing the torso when overlap remains legible.

### Reject

- Extra, missing, fused, or ambiguous fingers.
- Hands merging with clothing or props.
- Limbs that change ownership across overlaps.
- Repeated neutral arm positions across all expressions.
- Anatomically plausible but emotionally inactive poses.

## Pose and acting

The illustration should communicate dialogue function before facial details are inspected.

### Boke differentiation principles

**Recommended, not final identity:**

- more open or drifting gesture arcs;
- slightly less stable center of gravity;
- shapes that permit playful exaggeration;
- expressions that can hold confidence, confusion, delight, and accidental absurdity.

### Tsukkomi differentiation principles

**Recommended, not final identity:**

- clearer directional gestures and reaction angles;
- stronger stance or sharper shape rhythm;
- expressions that support disbelief, irritation, restraint, and decisive retort;
- pose language that can visually interrupt or correct the boke.

Do not reduce the distinction to “soft versus angry.” Both characters need a broad emotional range.

## Color

### Accept

- Limited palette with character-specific anchors.
- Flat fills with small controlled value steps.
- Background-independent edge readability.
- Accent colors used to separate roles or focus attention.

### Reject

- Dense multi-hue gradients.
- Airbrushed skin rendering.
- Excess bloom, rim light, chromatic aberration, or cinematic lighting.
- Uncontrolled color drift between variants.
- Palette differences that are the only character distinction.

### Recommended palette rules

- Record colors in a versioned palette specification.
- Use sRGB for delivery assets.
- Reserve one dominant, one support, and one accent family per character before adding exceptions.
- Evaluate grayscale readability and color-vision accessibility for major silhouette separations.

## Shading and texture

### Accept

- No shading, one hard-edged shadow shape, or sparse hatch/brush accents.
- Texture that follows material or gesture.
- Small intentional irregularities in fill edges where technically safe.

### Reject

- Smooth multi-step skin and hair gradients.
- Global soft-light rendering that changes the flat style.
- Repeated synthetic brush texture applied uniformly.
- Detailed rendering on minor areas that competes with the face and pose.

## Asymmetry

Asymmetry is required as an available design tool, not as a mandatory defect everywhere.

Positive examples:

- one shoulder higher during reaction;
- unequal hair masses;
- slightly different eye openness;
- off-center accessory placement;
- weight shifted to one leg;
- uneven hand openness.

Reject asymmetry that causes identity drift, accidental injury, or unreadable construction.

## Anti-AI-art review checklist

Score each item `pass`, `concern`, or `fail`. Any hard fail blocks approval.

### Construction

- [ ] No extra, missing, fused, or ambiguous limbs/fingers. **Hard fail**
- [ ] Overlaps and limb ownership are readable. **Hard fail**
- [ ] Face and costume anchors match the approved identity version. **Hard fail**
- [ ] Props and accessories have coherent geometry.

### Line and rendering

- [ ] Line weight is not mechanically uniform.
- [ ] Contours are not over-smoothed or generically vectorized.
- [ ] Detail density is intentionally distributed.
- [ ] Gradients and polished lighting do not override the flat style.

### Symmetry and templating

- [ ] Facial features are not perfectly mirrored.
- [ ] The pose has believable weight and asymmetry.
- [ ] Boke and tsukkomi do not share one recolored body/face template. **Hard fail**
- [ ] Repeated variants do not recycle identical hand and arm shapes without reason.

### Face and eyes

- [ ] Eye construction is character-specific.
- [ ] Highlights and pupils are not generic glossy-anime defaults.
- [ ] Expression affects brows, lids, cheeks, mouth, and head angle coherently.
- [ ] Identity remains stable across variants. **Hard fail**

### Hands and gesture

- [ ] Hand anatomy is readable. **Hard fail**
- [ ] Gesture supports the dialogue function.
- [ ] Hands are not hidden merely to avoid difficult construction.

### Color and asset integrity

- [ ] Palette matches the approved version.
- [ ] Transparent edges are clean without halos.
- [ ] No unexplained background fragments or embedded text. **Hard fail**
- [ ] The asset remains readable at target display size.

## Automated versus human checks

### Suitable for automation

- file format, dimensions, alpha channel, and color profile declaration;
- checksum and metadata completeness;
- unexpected background opacity;
- duplicate or near-duplicate candidate detection;
- palette range warnings;
- obvious symmetry or edge-density metrics used only as warnings;
- minimum silhouette separation and bounding-box checks.

### Human-only decisions

- whether irregularity feels intentional;
- whether the duo has distinctive appeal;
- whether acting supports a specific joke or retort;
- whether a design looks derivative or generic;
- whether identity drift is aesthetically significant;
- final style and commercial-use acceptance.

Automated metrics must never approve an image by themselves.

## Accept criteria

A candidate may be accepted when:

- no hard-fail condition is present;
- character identity is stable;
- role and emotion are readable at thumbnail size;
- line, color, and shading follow the flat rough style;
- asymmetry and deformation appear intentional;
- the candidate adds a useful pose or expression rather than duplicating an existing one;
- a human reviewer records approval.

## Reject criteria

Reject a candidate when any of the following applies:

- anatomy or overlap is ambiguous;
- character identity changes unintentionally;
- boke and tsukkomi become visually interchangeable;
- generic anime-eye or face-template appearance dominates;
- rendering is over-polished, gradient-heavy, or mechanically clean;
- the pose lacks readable acting;
- unexplained text, watermark, background fragment, or artifact appears;
- licensing or provenance is unknown for a production-use decision.

## Unresolved decisions

- Final face, hair, costume, accessories, and palettes.
- Degree of deformation for each character.
- Exact line medium to emulate or use.
- Whether approved source art will be raster, vector, or mixed.
- Whether mouth states will be layered separately.
- Final stage-side assignment.
