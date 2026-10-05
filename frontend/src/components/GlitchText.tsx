import { useEffect, useState } from 'react';
import './GlitchText.css';

type GlitchTextProps = {
  children: string;
  speed?: number;
  enableShadows?: boolean;
  enableOnHover?: boolean;
  /** Instead of hovering or glitching continuously, fire a glitch burst every `intervalMs`. */
  periodic?: boolean;
  /** Time between glitch bursts, in ms. Only used when `periodic` is true. */
  intervalMs?: number;
  /** How long each glitch burst lasts, in ms. Only used when `periodic` is true. */
  activeMs?: number;
  className?: string;
};

const GlitchText = ({
  children,
  speed = 0.5,
  enableShadows = true,
  enableOnHover = false,
  periodic = false,
  intervalMs = 4500,
  activeMs = 650,
  className = ''
}: GlitchTextProps) => {
  const inlineStyles = {
    '--after-duration': `${speed * 3}s`,
    '--before-duration': `${speed * 2}s`,
    '--after-shadow': enableShadows ? '-5px 0 red' : 'none',
    '--before-shadow': enableShadows ? '5px 0 cyan' : 'none'
  } as React.CSSProperties;

  const [isBursting, setIsBursting] = useState(false);

  useEffect(() => {
    if (!periodic) return;

    let burstTimeout: ReturnType<typeof setTimeout>;
    const interval = setInterval(() => {
      setIsBursting(true);
      burstTimeout = setTimeout(() => setIsBursting(false), activeMs);
    }, intervalMs);

    return () => {
      clearInterval(interval);
      clearTimeout(burstTimeout);
    };
  }, [periodic, intervalMs, activeMs]);

  const hoverClass = enableOnHover ? 'enable-on-hover' : '';
  const periodicClass = periodic ? 'periodic' : '';
  const activeClass = periodic && isBursting ? 'is-active' : '';

  return (
    <div
      className={`glitch ${hoverClass} ${periodicClass} ${activeClass} ${className}`.trim()}
      style={inlineStyles}
      data-text={children}
    >
      {children}
    </div>
  );
};

export default GlitchText;
