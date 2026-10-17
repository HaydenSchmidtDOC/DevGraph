import { forwardRef, memo } from 'react';
import styled from 'styled-components';

const Label = styled.span`
  font-weight: bold;
`;

export const Badge = memo(function Badge({ text }: { text: string }) {
  return <Label>{text}</Label>;
});

export const Field = forwardRef<HTMLInputElement, { name: string }>((props, ref) => (
  <label>
    <Label>{props.name}</Label>
    <input ref={ref} />
  </label>
));
