import React from 'react';
import {MemoryRouter} from 'react-router-dom';
import {LangProvider} from '@product/lib/i18n';
export function ProductContext({children}) {return <LangProvider><MemoryRouter initialEntries={['/admin/workbench/conversation/film-conversation']}><div className="product-root">{children}</div></MemoryRouter></LangProvider>}
