import React from "react";
import { Composition } from "remotion";
import { z } from "zod";
import { Captions } from "./Captions";
import { propsSchema, type CaptionProps } from "./core/schema";
import { getTemplate } from "./templates";
import demo from "../demo/props.json";

/**
 * One composition for every template: size, frame rate and length come from the props, and the props are
 * checked (shared schema, then the template's own parameter schema) before a single frame is drawn.
 */
export const Root: React.FC = () => {
  return (
    <Composition
      id="Captions"
      component={Captions}
      schema={propsSchema}
      defaultProps={demo as CaptionProps}
      width={1080}
      height={1920}
      fps={60}
      durationInFrames={600}
      calculateMetadata={({ props }) => {
        const parsed = propsSchema.safeParse(props);
        if (!parsed.success) throw new Error(`invalid caption props: ${z.prettifyError(parsed.error)}`);
        const p = parsed.data;
        const own = getTemplate(p.template).schema.safeParse(p.params);
        if (!own.success) throw new Error(`invalid parameters for template "${p.template}": ${z.prettifyError(own.error)}`);
        return { width: p.width, height: p.height, fps: p.fps, durationInFrames: p.durationInFrames, props: p };
      }}
    />
  );
};
