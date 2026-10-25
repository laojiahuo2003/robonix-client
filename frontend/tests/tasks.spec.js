const { test, expect } = require('@playwright/test');

async function prepare(page) {
  await page.route('**/api/**', route => {
    const url = route.request().url();
    const body = url.includes('/api/defaults') ? {} : url.includes('/api/settings') ? {ok:true,settings:{}} : {available:false};
    return route.fulfill({json:body});
  });
  await page.goto('/#dashboard');
  await page.waitForFunction(() => typeof renderGoalPanel === 'function' && state.activePage === 'dashboard');
}

async function populate(page, count = 12) {
  await page.evaluate(count => {
    state.taskState = {taskId:'T12',goal:'向后移动2米',status:'in_progress',tasks:Array.from({length:count},(_,i)=>({
      taskId:`T${i+1}`,goal:'确认厨房在当前场景中的位置（地图坐标系下的区域/目标点）'.repeat(3),status:i===0?'superseded':i===count-1?'in_progress':'done',
    }))};
    state.executorPlansReady = true;
    state.executorPlans = [{planId:'fixture-plan',ops:[{op_id:'fixture-op',description:'向后移动2米并验证位移',state:'running'}]}];
    renderGoalPanel();
  }, count);
}

test('long task lists scroll without overlapping the executor card or status', async ({page}) => {
  await prepare(page);
  await populate(page);
  const geometry = await page.evaluate(() => {
    const panel = document.querySelector('.current-goal-panel');
    const card = document.querySelector('.goal-preview').getBoundingClientRect();
    const rows = [...document.querySelectorAll('.task-list-row')];
    const first = rows[0].getBoundingClientRect();
    return {
      separated:first.top >= card.bottom,
      scrollable:document.querySelector('#taskList').scrollHeight > document.querySelector('#taskList').clientHeight,
      statusSeparated:rows.every(row=>row.querySelector('.task-list-goal').getBoundingClientRect().right <= row.querySelector('.task-list-status').getBoundingClientRect().left),
      ordered:rows.every((row,i)=>!i || row.getBoundingClientRect().top >= rows[i-1].getBoundingClientRect().bottom),
      activeColor:getComputedStyle(rows.at(-1).querySelector('.task-list-dot')).backgroundColor,
    };
  });
  expect(geometry).toEqual({separated:true,scrollable:true,statusSeparated:true,ordered:true,activeColor:'rgb(245, 158, 11)'});
  const rowHeights = await page.locator('.task-list-row').evaluateAll(rows => rows.map(row => row.getBoundingClientRect().height));
  expect(new Set(rowHeights).size).toBe(1);
  expect(rowHeights[0]).toBe(24);
  await page.locator('.task-list-row').last().scrollIntoViewIfNeeded();
  await expect(page.locator('.task-list-row').last()).toBeInViewport();
  await page.screenshot({path:test.info().outputPath('tasks-scrolled.png')});
  await page.evaluate(()=>window.RobonixI18N.setLang('zh'));
  await expect(page.locator('.task-list-status').first()).toHaveText('已替代');
  await populate(page,1);
  await expect(page.locator('#taskList')).toBeHidden();
});

test('task snapshots survive reload and conversation switching; new events replace them', async ({page}) => {
  await prepare(page);
  await populate(page,3);
  await page.evaluate(()=>{
    state.messages=[{id:'m1',role:'user',text:'测试任务'}];
    rememberLastSession(state.sessionId);
    persistCurrentConversation();
  });
  await page.reload();
  await expect(page.locator('.task-list-row')).toHaveCount(3);
  const id = await page.evaluate(()=>state.sessionId);
  await page.evaluate(()=>newSession());
  await expect(page.locator('#taskList')).toBeHidden();
  await page.evaluate(id=>openConversation(id), id);
  await expect(page.locator('.task-list-row')).toHaveCount(3);
  await page.evaluate(()=>handlePilotEvent({kind:'task_state',taskState:{...state.taskState,status:'done',tasks:state.taskState.tasks.map(t=>({...t,status:'done'}))}}));
  await expect(page.locator('.task-list-status').last()).toHaveText('done');
  await page.reload();
  await expect(page.locator('.task-list-status').last()).toHaveText('done');
});

test('other active tasks retain controls and abort updates every active row', async ({page}) => {
  await prepare(page);
  await page.evaluate(()=>handlePilotEvent({kind:'task_state',sessionId:'test-session',taskState:{taskId:'T2',goal:'second',status:'done',tasks:[{taskId:'T1',goal:'first',status:'in_progress'},{taskId:'T2',goal:'second',status:'done'}]}}));
  await expect(page.locator('#sendButton')).toHaveText(/Steer/);
  await expect(page.locator('#stopButton')).toBeVisible();
  await page.evaluate(()=>completeStopState());
  await expect(page.locator('.task-list-status').first()).toHaveText('canceled');
  await expect(page.locator('.task-list-status').last()).toHaveText('done');
  await expect(page.locator('#stopButton')).toBeHidden();
});



test('executor updates keep panel and tree positions stable', async ({page}) => {
  await prepare(page);
  await populate(page,12);
  const positions = () => page.evaluate(() => ['.current-goal-panel', '.goal-preview', '#taskList', '#sidebarMenu'].map(s => {
    const r=document.querySelector(s).getBoundingClientRect(); return [r.top,r.height];
  }));
  const before=await positions();
  await page.locator('#taskList').evaluate(e=>e.scrollTop=60);
  await page.evaluate(()=>{state.executorPlans[0].ops[0].description='A very long operation description '.repeat(30); renderGoalPanel();});
  expect(await positions()).toEqual(before);
  expect(await page.locator('#taskList').evaluate(e=>e.scrollTop)).toBe(60);
  await page.evaluate(()=>{state.executorPlans=[];renderGoalPanel();});
  expect(await positions()).toEqual(before);
  await populate(page,1);
  expect((await positions())[0]).toEqual(before[0]);
  expect((await positions())[3]).toEqual(before[3]);
  await page.locator(".goal-details summary").click();
  await page.evaluate(()=>renderGoalPanel());
  await expect(page.locator(".goal-details")).toHaveAttribute("open", "");
  await page.locator(".current-goal-panel").scrollIntoViewIfNeeded();
  await page.screenshot({path:test.info().outputPath('stable-panel.png')});
});
