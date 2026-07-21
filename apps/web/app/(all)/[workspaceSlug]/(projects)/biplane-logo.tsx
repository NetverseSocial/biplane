// biplane — the logo: a simple ascending biplane, drawn as inline SVG so it inherits the
// surrounding text colour (currentColor) and needs no asset pipeline. Side view, nose up.
export function BiplaneLogo({ size = 20, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-label="biplane logo"
      role="img"
    >
      <g transform="rotate(-20 24 26)" fill="currentColor" stroke="currentColor">
        {/* fuselage, nose to the right */}
        <path
          d="M9 26.5 L33 25 Q37.5 25.9 33.5 28.6 L10.5 30 Q8.2 28.4 9 26.5 Z"
          strokeWidth="0.6"
          strokeLinejoin="round"
        />
        {/* upper + lower wings */}
        <rect x="16.5" y="16.8" width="14.5" height="2.7" rx="1.35" strokeWidth="0" />
        <rect x="16.5" y="30.6" width="14.5" height="2.7" rx="1.35" strokeWidth="0" />
        {/* struts */}
        <path d="M19.6 19.5 V30.8 M27.9 19.3 V30.6" strokeWidth="1.1" strokeLinecap="round" fill="none" />
        {/* tail fin + tailplane */}
        <path d="M9.6 26.6 L5.6 18.6 L12.4 24.7 Z" strokeWidth="0.6" strokeLinejoin="round" />
        <rect x="5.2" y="26.2" width="6.4" height="1.8" rx="0.9" strokeWidth="0" />
        {/* propeller: hub + blade arc */}
        <circle cx="37.6" cy="26.6" r="1.5" strokeWidth="0" />
        <path d="M38.6 20.6 Q40.4 26.6 38.6 32.6" strokeWidth="1.2" strokeLinecap="round" fill="none" />
      </g>
      {/* ascent trail */}
      <path
        d="M4 44 Q14 42 22 37.5"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeDasharray="3 3"
        fill="none"
        opacity="0.55"
      />
    </svg>
  );
}
