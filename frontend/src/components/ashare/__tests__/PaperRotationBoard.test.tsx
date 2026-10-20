import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {PaperRotationBoard,type Rotation} from '../PaperRotationBoard';
afterEach(cleanup);
const hash='a'.repeat(64),source='b'.repeat(64);
const rotation:Rotation={mode:'auto',margin_points:10,cooldown_minutes:30,max_daily_switches:2,ranking:['600001.SH'],
  proposals:{'600001.SH':{symbol:'600001.SH',name:'新候选',source_hash:source,rank:1,score:60,eligible:false,reviewed:false,
    replace_name:'旧观察',replace_symbol:'600000.SH',reason:'同口径比较',blockers:['原文待核验'],created_at:'2026-10-21T10:00:00+08:00',evidence:{},events:[],announcements:[]}},history:[]};
it('keeps the mode change scoped to the pinned virtual portfolio',()=>{
  const action=vi.fn().mockResolvedValue(undefined);render(<PaperRotationBoard hash={hash} busy={false} onAction={action}/>);
  fireEvent.change(screen.getByLabelText('模拟轮换方式'),{target:{value:'auto'}});
  expect(action).toHaveBeenCalledWith('/paper/rotation','PUT',{mode:'auto',expected_plan_hash:hash});
  expect(screen.getByText(/原五股预测、原排名和实际账户独立保留/)).toBeInTheDocument();
});
it('does not precheck source review or expose a confirmation before all gates pass',()=>{
  const action=vi.fn().mockResolvedValue(undefined);render(<PaperRotationBoard rotation={{...rotation,mode:'confirm'}} hash={hash} busy={false} onAction={action}/>);
  expect(screen.getByRole('button',{name:'保存本次原文核对'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'确认本次模拟替换'})).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button',{name:'保存本次原文核对'}));
  expect(action).toHaveBeenCalledWith('/paper/candidate','POST',{symbol:'600001.SH',source_hash:source,action:'review'});
});
